"""Phase 8.9 — visual Family History Event-card landing page."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget, QInputDialog,
)

from ppa import catalogue
from ppa.event_home import EventHomeCard, EventHomeView
from ppa.event_health import EventHealthView
from ppa.event_search import EventSearchIndex, build_event_search_facets, search_event_index
from ppa.event_views import delete_event_view, get_event_view, list_event_views, save_event_view
from ppa.event_activity import continue_event_id, get_event_activity, list_favorite_event_ids, list_recent_event_ids
from ppa.timeline_scale import page_items
from ppa.ui.workers import ThumbnailWorker, WorkerRegistry

_EVENT_ID_ROLE = Qt.ItemDataRole.UserRole
_COVER_ID_ROLE = Qt.ItemDataRole.UserRole + 1


class EventHomeDialog(QDialog):
    """Read-only visual index into durable human Event stories."""
    thumbnail_request = Signal(str, str, str, bool)
    _PAGE_SIZE = 30

    def __init__(self, conn, timeline_view, home: EventHomeView, search_index: EventSearchIndex, health_view: EventHealthView, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._timeline_view = timeline_view
        self._home = home
        self._search_index = search_index
        self._search_hits = {}
        self._cache_dir = Path(cache_dir or Path.home() / ".cache" / "personal-photo-archive" / "event-home")
        self._registry = WorkerRegistry()
        self._year: int | None = None
        self._page = 0
        self._visible: tuple[EventHomeCard, ...] = home.cards
        self._items_by_cover: dict[str, list[QListWidgetItem]] = {}
        self._facets = build_event_search_facets(search_index)
        self._saved_views = ()
        self._health = {item.event_id: item for item in health_view.events}

        self.setWindowTitle("Family History")
        self.resize(1220, 820)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        title = QLabel("Family History")
        title.setObjectName("SectionHeader")
        root.addWidget(title)
        subtitle = QLabel(
            "Human-authored Events grouped by year. Cover images are stable browsing defaults only; "
            "they do not imply historical importance or chronology authority.")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("FieldKey")
        root.addWidget(subtitle)

        body = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(body, 1)

        nav_wrap = QWidget(); nav_l = QVBoxLayout(nav_wrap); nav_l.setContentsMargins(0,0,0,0)
        nav_l.addWidget(QLabel("Browse"))
        self._activity_filter = QComboBox()
        self._activity_filter.addItem("All Events", "all")
        self._activity_filter.addItem("★ Favourites", "favorites")
        self._activity_filter.addItem("Recently Viewed", "recent")
        self._activity_filter.addItem("Needs Attention", "attention")
        self._activity_filter.addItem("Curation Complete", "complete")
        self._activity_filter.currentIndexChanged.connect(self._search_changed)
        nav_l.addWidget(self._activity_filter)
        self._continue = QPushButton("Continue where I left off…")
        self._continue.clicked.connect(self._continue_story)
        nav_l.addWidget(self._continue)
        nav_l.addSpacing(8)
        nav_l.addWidget(QLabel("Saved views"))
        self._saved = QComboBox(); self._saved.currentIndexChanged.connect(self._saved_view_changed)
        nav_l.addWidget(self._saved)
        saved_buttons = QHBoxLayout()
        self._save_view = QPushButton("Save…"); self._save_view.clicked.connect(self._save_current_view)
        self._delete_view = QPushButton("Delete"); self._delete_view.clicked.connect(self._delete_current_view)
        saved_buttons.addWidget(self._save_view); saved_buttons.addWidget(self._delete_view)
        nav_l.addLayout(saved_buttons)
        nav_l.addSpacing(8)
        nav_l.addWidget(QLabel("Years"))
        self._years = QListWidget(); self._years.setMaximumWidth(190)
        self._years.currentItemChanged.connect(self._year_changed)
        nav_l.addWidget(self._years, 1)
        body.addWidget(nav_wrap)

        centre = QWidget(); cl = QVBoxLayout(centre); cl.setContentsMargins(8,0,8,0)
        search_row = QHBoxLayout()
        self._search = QLineEdit(); self._search.setPlaceholderText("Search event names, people, places, occasions, descriptions and stories…")
        self._search.setClearButtonEnabled(True); self._search.textChanged.connect(self._search_changed)
        search_row.addWidget(self._search, 1)
        self._from_date = QLineEdit(); self._from_date.setPlaceholderText("From YYYY-MM-DD"); self._from_date.setMaximumWidth(125); self._from_date.editingFinished.connect(self._search_changed)
        self._to_date = QLineEdit(); self._to_date.setPlaceholderText("To YYYY-MM-DD"); self._to_date.setMaximumWidth(125); self._to_date.editingFinished.connect(self._search_changed)
        search_row.addWidget(self._from_date); search_row.addWidget(self._to_date)
        self._search_status = QLabel(""); self._search_status.setObjectName("FieldKey"); search_row.addWidget(self._search_status)
        cl.addLayout(search_row)
        facet_row = QHBoxLayout()
        self._occasion = QComboBox(); self._place = QComboBox(); self._person = QComboBox()
        for combo, label in ((self._occasion, "Any occasion"), (self._place, "Any place"), (self._person, "Any person/group")):
            combo.addItem(label, None); combo.currentIndexChanged.connect(self._search_changed); facet_row.addWidget(combo)
        for f in self._facets.occasions: self._occasion.addItem(f"{f.value} ({f.count})", f.value)
        for f in self._facets.places: self._place.addItem(f"{f.value} ({f.count})", f.value)
        for f in self._facets.people: self._person.addItem(f"{f.value} ({f.count})", f.value)
        cl.addLayout(facet_row)
        self._scope = QLabel(""); self._scope.setObjectName("FieldKey"); cl.addWidget(self._scope)
        self._cards = QListWidget()
        self._cards.setViewMode(QListWidget.ViewMode.IconMode)
        self._cards.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._cards.setMovement(QListWidget.Movement.Static)
        self._cards.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._cards.setWordWrap(True)
        self._cards.setIconSize(QSize(230, 150))
        self._cards.setGridSize(QSize(340, 275))
        self._cards.setSpacing(10)
        self._cards.currentItemChanged.connect(self._show_detail)
        self._cards.itemDoubleClicked.connect(lambda item: self._open_story_item(item))
        cl.addWidget(self._cards, 1)
        page = QHBoxLayout()
        self._prev = QPushButton("◀ Previous"); self._prev.clicked.connect(self._page_previous)
        self._page_label = QLabel(""); self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next = QPushButton("Next ▶"); self._next.clicked.connect(self._page_next)
        page.addWidget(self._prev); page.addWidget(self._page_label, 1); page.addWidget(self._next)
        cl.addLayout(page)
        body.addWidget(centre)

        side = QWidget(); sl = QVBoxLayout(side); sl.setContentsMargins(8,0,0,0)
        heading = QLabel("Event"); heading.setObjectName("SectionHeader"); sl.addWidget(heading)
        self._detail = QLabel("Select an Event to see its story summary.")
        self._detail.setWordWrap(True); self._detail.setAlignment(Qt.AlignmentFlag.AlignTop); sl.addWidget(self._detail,1)
        self._open = QPushButton("Open story…"); self._open.setEnabled(False); self._open.clicked.connect(self._open_selected)
        sl.addWidget(self._open)
        body.addWidget(side)
        body.setSizes([170, 790, 260])

        bottom = QHBoxLayout(); bottom.addStretch(1)
        close = QPushButton("Close"); close.clicked.connect(self.close); bottom.addWidget(close); root.addLayout(bottom)

        worker = ThumbnailWorker(self._cache_dir, size=240)
        worker.ready.connect(self._thumbnail_ready, Qt.ConnectionType.QueuedConnection)
        self._registry.start_persistent(worker)
        self.thumbnail_request.connect(worker.request, Qt.ConnectionType.QueuedConnection)

        self._refresh_saved_views()
        self._populate_years()

    def _populate_years(self) -> None:
        self._years.clear()
        all_item = QListWidgetItem(f"All Events ({len(self._home.cards)})")
        all_item.setData(Qt.ItemDataRole.UserRole, None); self._years.addItem(all_item)
        for group in self._home.years:
            item = QListWidgetItem(f"{group.year} ({group.count})")
            item.setData(Qt.ItemDataRole.UserRole, group.year); self._years.addItem(item)
        self._years.setCurrentRow(0)

    def _year_changed(self, current, _previous) -> None:
        self._year = current.data(Qt.ItemDataRole.UserRole) if current else None
        self._page = 0
        self._apply_filters()


    def _search_changed(self, _text: str | None = None) -> None:
        self._page = 0
        self._apply_filters()

    def _apply_filters(self) -> None:
        try:
            results = search_event_index(
                self._search_index, text=self._search.text(), year=self._year,
                start_date=self._from_date.text().strip() or None,
                end_date=self._to_date.text().strip() or None,
                occasion=self._occasion.currentData(), place=self._place.currentData(),
                person=self._person.currentData(),
            )
        except ValueError as exc:
            self._search_status.setText(str(exc)); return
        self._search_hits = {h.event_id: h for h in results.hits}
        cards = tuple(self._home.card(h.event_id) for h in results.hits)
        mode = self._activity_filter.currentData()
        if mode == "favorites":
            allowed = set(list_favorite_event_ids(self._conn, library_id=self._home.library_id))
            cards = tuple(c for c in cards if c.event_id in allowed)
        elif mode == "recent":
            order = list_recent_event_ids(self._conn, library_id=self._home.library_id, limit=100)
            by_id = {c.event_id: c for c in cards}
            cards = tuple(by_id[eid] for eid in order if eid in by_id)
        elif mode == "attention":
            cards = tuple(c for c in cards if (self._health.get(c.event_id) and
                          self._health[c.event_id].needs_attention))
        elif mode == "complete":
            cards = tuple(c for c in cards if self._health.get(c.event_id) and self._health[c.event_id].curation_complete)
        self._visible = cards
        self._continue.setEnabled(continue_event_id(self._conn, library_id=self._home.library_id) is not None)
        self._search_status.setText(f"{results.total} match{'es' if results.total != 1 else ''}" if results.query else "")
        self._render_page()

    def _card_text(self, card: EventHomeCard) -> str:
        counts = card.lane_counts
        chronology = f"{counts['placed']} placed · {counts['range']} ranges · {counts['tentative']} tentative · {counts['unplaced']} unplaced"
        snippet = f"\n{card.snippet}" if card.snippet else ""
        where = " · ".join(x for x in (card.occasion_text, card.place_text) if x)
        where = f"\n{where}" if where else ""
        star = "★ " if get_event_activity(self._conn, card.event_id).favorite else ""
        health = self._health.get(card.event_id)
        badges = ""
        if health:
            primary = [b for b in health.badges if b in ("Curation complete", "Needs chronology review", "Needs story", "Custom cover")]
            if primary:
                badges = "\n" + " · ".join(primary)
        return f"{star}{card.name}\n{card.date_label}\n{card.member_count} photos · {chronology}{badges}{where}{snippet}"

    def _render_page(self) -> None:
        page = page_items(self._visible, page=self._page, page_size=self._PAGE_SIZE)
        self._page = page.page
        self._cards.clear(); self._items_by_cover = {}
        for card in page.items:
            item = QListWidgetItem(self._card_text(card))
            item.setData(_EVENT_ID_ROLE, card.event_id)
            item.setData(_COVER_ID_ROLE, card.cover_file_id)
            hit = self._search_hits.get(card.event_id)
            match_note = f" Search matched: {', '.join(hit.matched_fields)}." if hit and hit.matched_fields else ""
            item.setToolTip("Cover is a deterministic browsing default, not a semantic selection." + match_note)
            self._cards.addItem(item)
            if card.cover_file_id:
                self._items_by_cover.setdefault(card.cover_file_id, []).append(item)

        cover_ids = tuple(self._items_by_cover)
        by_id = {g.file_id: g for g in catalogue.grid_items_for_files(self._conn, cover_ids)}
        for fid in cover_ids:
            grid = by_id.get(fid)
            if grid is not None:
                self.thumbnail_request.emit(
                    grid.file_id, grid.path, grid.sha256 or "",
                    grid.health_status != "hash_mismatch",
                )

        self._page_label.setText(
            f"{page.start_index + 1 if page.total_items else 0}–{page.end_index} of {page.total_items} · page {page.page + 1}/{page.total_pages}")
        self._prev.setEnabled(page.has_previous); self._next.setEnabled(page.has_next)
        scope = "All Events" if self._year is None else str(self._year)
        query = "" if not self._search.text().strip() else f" · search: {self._search.text().strip()}"
        dates = ""
        if self._from_date.text().strip() or self._to_date.text().strip():
            dates = f" · dates: {self._from_date.text().strip() or '…'} → {self._to_date.text().strip() or '…'}"
        facets = [x for x in (self._occasion.currentData(), self._place.currentData(), self._person.currentData()) if x]
        facet_text = f" · facets: {', '.join(facets)}" if facets else ""
        self._scope.setText(f"{scope}{query}{dates}{facet_text} · {len(self._visible)} event{'s' if len(self._visible) != 1 else ''} · at most {self._PAGE_SIZE} covers decoded per page")
        self._detail.setText("Select an Event to see its story summary."); self._open.setEnabled(False)

    def _refresh_saved_views(self, select_id: str | None = None) -> None:
        self._saved.blockSignals(True)
        self._saved.clear(); self._saved.addItem("Current / unsaved", None)
        self._saved_views = list_event_views(self._conn, library_id=self._home.library_id)
        selected = 0
        for idx, view in enumerate(self._saved_views, start=1):
            self._saved.addItem(view.name, view.id)
            if select_id == view.id: selected = idx
        self._saved.setCurrentIndex(selected)
        self._saved.blockSignals(False)
        self._delete_view.setEnabled(self._saved.currentData() is not None)

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: str | None) -> None:
        target = (value or "").casefold()
        for i in range(combo.count()):
            data = combo.itemData(i)
            if (data or "").casefold() == target:
                combo.setCurrentIndex(i); return
        combo.setCurrentIndex(0)

    def _saved_view_changed(self, _index: int) -> None:
        view_id = self._saved.currentData()
        self._delete_view.setEnabled(view_id is not None)
        if not view_id:
            return
        try:
            view = get_event_view(self._conn, str(view_id))
        except ValueError as exc:
            QMessageBox.warning(self, "Saved view", str(exc)); self._refresh_saved_views(); return
        self._search.blockSignals(True); self._search.setText(view.query_text); self._search.blockSignals(False)
        self._from_date.setText(view.start_date or ""); self._to_date.setText(view.end_date or "")
        # Year navigator is the canonical year control.
        row = 0
        if view.year is not None:
            for i in range(self._years.count()):
                if self._years.item(i).data(Qt.ItemDataRole.UserRole) == view.year:
                    row = i; break
        self._years.setCurrentRow(row)
        self._select_combo_data(self._occasion, view.occasion_filter)
        self._select_combo_data(self._place, view.place_filter)
        self._select_combo_data(self._person, view.person_filter)
        self._page = 0; self._apply_filters()

    def _save_current_view(self) -> None:
        default = self._saved.currentText() if self._saved.currentData() else ""
        name, ok = QInputDialog.getText(self, "Save Event view", "View name:", text=default)
        if not ok: return
        try:
            view = save_event_view(self._conn, library_id=self._home.library_id, name=name,
                                    query_text=self._search.text(), year=self._year,
                                    start_date=self._from_date.text().strip() or None,
                                    end_date=self._to_date.text().strip() or None,
                                    occasion_filter=self._occasion.currentData(),
                                    place_filter=self._place.currentData(),
                                    person_filter=self._person.currentData())
        except (ValueError, Exception) as exc:
            QMessageBox.warning(self, "Save Event view", str(exc)); return
        self._refresh_saved_views(view.id)

    def _delete_current_view(self) -> None:
        view_id = self._saved.currentData()
        if not view_id: return
        if QMessageBox.question(self, "Delete saved view", f"Delete saved view ‘{self._saved.currentText()}’?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        delete_event_view(self._conn, str(view_id)); self._refresh_saved_views()

    @Slot(str, object)
    def _thumbnail_ready(self, file_id: str, image) -> None:
        icon = QIcon(QPixmap.fromImage(image))
        for item in self._items_by_cover.get(file_id, ()):
            item.setIcon(icon)

    def _card_for_item(self, item: QListWidgetItem | None) -> EventHomeCard | None:
        if item is None: return None
        eid = item.data(_EVENT_ID_ROLE)
        return self._home.card(str(eid)) if eid else None

    def _show_detail(self, current, _previous) -> None:
        card = self._card_for_item(current)
        self._open.setEnabled(card is not None)
        if card is None: return
        c = card.lane_counts
        cover = "No visible member" if card.cover_file_id is None else f"stable default ({card.cover_rule.replace('_',' ')})"
        health = self._health.get(card.event_id)
        health_text = ""
        if health:
            health_text = "\n\nCuration health\n" + ("\n".join(f"• {b}" for b in health.badges) if health.badges else "No attention indicators")
        self._detail.setText(
            f"{card.name}\n\n{card.date_label}\n{card.member_count} event members · {card.visible_member_count} visible in current Timeline\n\n"
            f"Chronology now\nPlaced: {c['placed']}\nRanges: {c['range']}\nTentative: {c['tentative']}\nUnplaced: {c['unplaced']}\n\n"
            f"Cover: {cover}{health_text}\n\n{card.snippet or 'No story description yet.'}")

    def _page_previous(self) -> None:
        self._page = max(0, self._page - 1); self._render_page()

    def _page_next(self) -> None:
        page = page_items(self._visible, page=self._page, page_size=self._PAGE_SIZE)
        if page.has_next: self._page += 1; self._render_page()

    def _open_story_item(self, item: QListWidgetItem) -> None:
        card = self._card_for_item(item)
        if card is not None: self._open_story(card.event_id)

    def _open_selected(self) -> None:
        card = self._card_for_item(self._cards.currentItem())
        if card is not None: self._open_story(card.event_id)

    def _open_story(self, event_id: str) -> None:
        from ppa.ui.event_story_dialog import EventStoryDialog
        dialog = EventStoryDialog(self._conn, self._timeline_view, event_id, self, cache_dir=self._cache_dir / "stories")
        dialog.show()
        self._story_dialog = dialog


    def _continue_story(self) -> None:
        event_id = continue_event_id(self._conn, library_id=self._home.library_id)
        if event_id is not None:
            self._open_story(event_id)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._registry.shutdown(); super().closeEvent(event)
