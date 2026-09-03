"""Phase 8.1 visual, read-only chronology timeline browser."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QListView, QListWidget,
    QListWidgetItem, QPushButton, QSlider, QSplitter, QTabWidget, QVBoxLayout, QWidget, QInputDialog, QMessageBox, QLineEdit, QTextEdit,
)

from ppa import catalogue
from ppa.timeline_scale import (
    DEFAULT_PAGE_SIZE, density_buckets, filter_for_bucket, page_for_fraction, page_items,
)
from ppa.timeline_clusters import build_clusters, items_for_cluster
from ppa.events import (create_event_from_cluster, items_for_event, list_events, rename_event,
    add_event_member, remove_event_member, update_event_note, list_event_history,
    get_event_context, update_event_context, list_event_context_history,
    get_event_presentation, set_event_cover, set_event_presentation_order,
    reset_event_presentation, list_event_presentation_history)
from ppa.ui import theme
from ppa.ui.delegate import PhotoTileDelegate
from ppa.ui.models import FILE_ID_ROLE, PhotoGridModel
from ppa.ui.workers import ThumbnailWorker, WorkerRegistry


_LANES = (
    ("placed", "Placed"),
    ("range", "Ranges"),
    ("tentative", "Tentative"),
    ("unplaced", "Unplaced"),
)



class EventEditorDialog(QDialog):
    """Explicit human curation for one durable Event.

    Membership edits affect only the Event interpretation object. They never
    move a photo between Timeline lanes or alter chronology evidence.
    """
    _MAX_CANDIDATES = 300

    def __init__(self, conn, event, view, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._event = event
        self._view = view
        self.setWindowTitle(f"Edit event — {event.name}")
        self.resize(900, 650)
        root = QVBoxLayout(self)

        form = QHBoxLayout()
        self._name = QLineEdit(event.name)
        self._note = QTextEdit(event.note or "")
        self._note.setMaximumHeight(70)
        form.addWidget(QLabel("Name:")); form.addWidget(self._name, 1)
        root.addLayout(form)
        root.addWidget(QLabel("Quick curation note (does not alter chronology):"))
        root.addWidget(self._note)

        context = get_event_context(conn, event.id)
        root.addWidget(QLabel("Story context — human-authored memory, never chronology evidence"))
        self._occasion = QLineEdit(context.occasion_text or ""); self._occasion.setPlaceholderText("Occasion / context, e.g. Christmas Day")
        self._place = QLineEdit(context.place_text or ""); self._place.setPlaceholderText("Place as remembered, e.g. Mum and Dad's house")
        self._people = QLineEdit(context.people_text or ""); self._people.setPlaceholderText("People / relationships as remembered")
        self._description = QTextEdit(context.description or ""); self._description.setMaximumHeight(70); self._description.setPlaceholderText("Short description")
        self._story = QTextEdit(context.story_text or ""); self._story.setMaximumHeight(100); self._story.setPlaceholderText("Longer memory / story")
        root.addWidget(self._occasion); root.addWidget(self._place); root.addWidget(self._people)
        root.addWidget(self._description); root.addWidget(self._story)
        save = QPushButton("Save event details / story context")
        save.clicked.connect(self._save_details)
        root.addWidget(save)

        body = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(body, 1)

        left = QWidget(); ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Event members"))
        self._members = QListWidget(); ll.addWidget(self._members, 1)
        presentation = QHBoxLayout()
        self._cover = QPushButton("Use selected as cover")
        self._cover.clicked.connect(self._set_selected_cover)
        self._up = QPushButton("Move up"); self._up.clicked.connect(lambda: self._move_selected(-1))
        self._down = QPushButton("Move down"); self._down.clicked.connect(lambda: self._move_selected(1))
        presentation.addWidget(self._cover); presentation.addWidget(self._up); presentation.addWidget(self._down)
        ll.addLayout(presentation)
        save_order = QPushButton("Save presentation order")
        save_order.clicked.connect(self._save_presentation_order); ll.addWidget(save_order)
        reset_presentation = QPushButton("Reset presentation to defaults")
        reset_presentation.clicked.connect(self._reset_presentation); ll.addWidget(reset_presentation)
        remove = QPushButton("Remove selected member")
        remove.clicked.connect(self._remove_selected); ll.addWidget(remove)
        body.addWidget(left)

        right = QWidget(); rl = QVBoxLayout(right)
        rl.addWidget(QLabel("Add photo from current Timeline catalogue"))
        self._search = QLineEdit(); self._search.setPlaceholderText("Filter by filename, date, or lane…")
        self._search.textChanged.connect(self._populate_candidates); rl.addWidget(self._search)
        self._candidates = QListWidget(); rl.addWidget(self._candidates, 1)
        self._candidate_hint = QLabel(""); self._candidate_hint.setObjectName("FieldKey"); rl.addWidget(self._candidate_hint)
        add = QPushButton("Add selected photo to event")
        add.clicked.connect(self._add_selected); rl.addWidget(add)
        body.addWidget(right)

        root.addWidget(QLabel("Recent curation history"))
        self._history = QListWidget(); self._history.setMaximumHeight(130); root.addWidget(self._history)
        close = QPushButton("Close"); close.clicked.connect(self.accept); root.addWidget(close)
        self._reload()

    def _item_label(self, item) -> str:
        when = item.start_date or "unplaced"
        if item.end_date:
            when = f"{when} → {item.end_date}"
        return f"{when} · {item.lane} · {item.filename}"

    def _reload(self) -> None:
        from ppa.events import get_event
        self._event = get_event(self._conn, self._event.id)
        self._members.clear()
        by_id = {i.file_id: i for i in self._view.items}
        pref = get_event_presentation(self._conn, self._event.id)
        ordered_ids = pref.order_file_ids or self._event.file_ids
        for fid in ordered_ids:
            item = by_id.get(fid)
            cover = " · ★ preferred cover" if pref.cover_file_id == fid else ""
            row = QListWidgetItem((self._item_label(item) if item else f"catalogued member · {fid}") + cover)
            row.setData(Qt.ItemDataRole.UserRole, fid)
            self._members.addItem(row)
        self._populate_candidates()
        self._history.clear()
        combined = []
        for h in list_event_history(self._conn, self._event.id):
            suffix = f" · {h.file_id}" if h.file_id else ""
            combined.append((h.created_at, f"{h.created_at} · {h.action}{suffix}"))
        for h in list_event_context_history(self._conn, self._event.id):
            combined.append((h.created_at, f"{h.created_at} · story_context"))
        for h in list_event_presentation_history(self._conn, self._event.id):
            combined.append((h.created_at, f"{h.created_at} · presentation_{h.action}"))
        for _, text in sorted(combined)[-20:]:
            self._history.addItem(text)

    def _populate_candidates(self) -> None:
        if not hasattr(self, '_candidates'):
            return
        query = self._search.text().strip().casefold()
        members = set(self._event.file_ids)
        rows = []
        for item in self._view.items:
            if item.file_id in members:
                continue
            label = self._item_label(item)
            if query and query not in label.casefold():
                continue
            rows.append((label, item.file_id))
            if len(rows) >= self._MAX_CANDIDATES:
                break
        self._candidates.clear()
        for label, fid in rows:
            row = QListWidgetItem(label); row.setData(Qt.ItemDataRole.UserRole, fid); self._candidates.addItem(row)
        self._candidate_hint.setText(
            f"Showing {len(rows)} candidate photos" +
            (f" (limited to {self._MAX_CANDIDATES}; filter to narrow)" if len(rows) >= self._MAX_CANDIDATES else "")
        )

    def _save_details(self) -> None:
        try:
            rename_event(self._conn, self._event.id, self._name.text())
            update_event_note(self._conn, self._event.id, self._note.toPlainText())
            update_event_context(
                self._conn, self._event.id,
                description=self._description.toPlainText(),
                place_text=self._place.text(),
                people_text=self._people.text(),
                occasion_text=self._occasion.text(),
                story_text=self._story.toPlainText(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Event", str(exc)); return
        self._reload()


    def _set_selected_cover(self) -> None:
        row = self._members.currentItem()
        if row is None:
            return
        try:
            set_event_cover(self._conn, self._event.id, str(row.data(Qt.ItemDataRole.UserRole)))
        except ValueError as exc:
            QMessageBox.warning(self, "Event presentation", str(exc)); return
        self._reload()

    def _move_selected(self, delta: int) -> None:
        row = self._members.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self._members.count():
            return
        item = self._members.takeItem(row)
        self._members.insertItem(target, item)
        self._members.setCurrentRow(target)

    def _save_presentation_order(self) -> None:
        ids = tuple(str(self._members.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self._members.count()))
        try:
            set_event_presentation_order(self._conn, self._event.id, ids)
        except ValueError as exc:
            QMessageBox.warning(self, "Event presentation", str(exc)); return
        self._reload()

    def _reset_presentation(self) -> None:
        try:
            reset_event_presentation(self._conn, self._event.id)
        except ValueError as exc:
            QMessageBox.warning(self, "Event presentation", str(exc)); return
        self._reload()

    def _add_selected(self) -> None:
        row = self._candidates.currentItem()
        if row is None:
            return
        try:
            add_event_member(self._conn, self._event.id, str(row.data(Qt.ItemDataRole.UserRole)))
        except ValueError as exc:
            QMessageBox.warning(self, "Event", str(exc)); return
        self._reload()

    def _remove_selected(self) -> None:
        row = self._members.currentItem()
        if row is None:
            return
        fid = str(row.data(Qt.ItemDataRole.UserRole))
        if QMessageBox.question(self, "Remove event member",
                                "Remove this photo from the human Event? Its chronology will not change.") != QMessageBox.StandardButton.Yes:
            return
        try:
            remove_event_member(self._conn, self._event.id, fid)
        except ValueError as exc:
            QMessageBox.warning(self, "Event", str(exc)); return
        self._reload()


class TimelineDialog(QDialog):
    """Visual timeline over the already-authorised Phase-8 projection.

    The dialog never re-evaluates chronology.  Navigation and lane changes only
    filter the immutable TimelineView passed in by the background worker.
    """

    def __init__(self, conn, view, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._view = view
        self._cache_dir = Path(cache_dir or Path.home() / ".cache" / "personal-photo-archive" / "timeline")
        self._registry = WorkerRegistry()
        self._models: dict[str, PhotoGridModel] = {}
        self._views: dict[str, QListView] = {}
        self._visible_items: dict[str, tuple] = {}
        self._nav_key = "all"
        self._scale = "year"
        self._page_size = DEFAULT_PAGE_SIZE
        self._lane_pages: dict[str, int] = {lane: 0 for lane, _ in _LANES}
        self._selected_item = None
        self._clusters = build_clusters(view)
        self._cluster_by_key = {c.key: c for c in self._clusters.clusters}
        self._events = list_events(conn, library_id=view.scope.library_id)
        self._event_by_id = {e.id: e for e in self._events}

        self.setWindowTitle("Chronology Timeline")
        self.resize(1180, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        counts = view.lanes
        self._summary = QLabel(
            f"{len(view.items)} photos · {counts['placed'].count} placed · "
            f"{counts['range'].count} ranges · {counts['tentative'].count} tentative · "
            f"{counts['unplaced'].count} unplaced")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        note = QLabel(
            "Timeline is read-only. Confirmed/current chronology is separated from tentative proposals; "
            "ranges retain their uncertainty and stale interpretations never place a photo.")
        note.setWordWrap(True)
        note.setObjectName("FieldKey")
        root.addWidget(note)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        # Left chronology navigator -------------------------------------------------
        nav_panel = QWidget()
        nav_layout = QVBoxLayout(nav_panel)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_title = QLabel("Browse chronology")
        nav_title.setObjectName("SectionHeader")
        nav_layout.addWidget(nav_title)
        self._scale_combo = QComboBox()
        self._scale_combo.addItem("Decades", "decade")
        self._scale_combo.addItem("Years", "year")
        self._scale_combo.addItem("Months", "month")
        self._scale_combo.addItem("Clusters", "cluster")
        self._scale_combo.addItem("Events", "event")
        self._scale_combo.setCurrentIndex(1)
        self._scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        nav_layout.addWidget(self._scale_combo)
        self._nav = QListWidget()
        self._nav.setMinimumWidth(175)
        self._nav.currentItemChanged.connect(self._on_nav_changed)
        nav_layout.addWidget(self._nav, 1)
        self._density_hint = QLabel("Density: —")
        self._density_hint.setObjectName("FieldKey")
        self._density_hint.setWordWrap(True)
        nav_layout.addWidget(self._density_hint)
        splitter.addWidget(nav_panel)

        # Centre visual lanes -------------------------------------------------------
        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        self._scope_label = QLabel("")
        self._scope_label.setObjectName("FieldKey")
        centre_layout.addWidget(self._scope_label)

        self._tabs = QTabWidget()
        # Do not connect currentChanged until every dependent control exists.
        # addTab() can emit currentChanged during construction; connecting here
        # previously allowed _on_tab_changed() to touch _open_btn before the
        # right-hand detail panel had created it.
        centre_layout.addWidget(self._tabs, 1)

        page_row = QHBoxLayout()
        self._prev_page = QPushButton("◀ Previous")
        self._prev_page.clicked.connect(self._page_previous)
        self._page_label = QLabel("Page 1 / 1")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scrubber = QSlider(Qt.Orientation.Horizontal)
        self._scrubber.setRange(0, 1000)
        self._scrubber.setValue(0)
        self._scrubber.sliderReleased.connect(self._scrub_to_position)
        self._next_page = QPushButton("Next ▶")
        self._next_page.clicked.connect(self._page_next)
        page_row.addWidget(self._prev_page)
        page_row.addWidget(self._page_label)
        page_row.addWidget(self._scrubber, 1)
        page_row.addWidget(self._next_page)
        centre_layout.addLayout(page_row)
        for lane, label in _LANES:
            view_widget = QListView()
            view_widget.setViewMode(QListView.ViewMode.IconMode)
            view_widget.setResizeMode(QListView.ResizeMode.Adjust)
            view_widget.setMovement(QListView.Movement.Static)
            view_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            view_widget.setUniformItemSizes(True)
            view_widget.setSpacing(6)
            view_widget.setIconSize(QSize(150, 150))
            view_widget.setGridSize(QSize(180, 205))
            view_widget.setItemDelegate(PhotoTileDelegate(view_widget))
            view_widget.doubleClicked.connect(self._open_index)
            view_widget.clicked.connect(self._show_index_detail)

            model = PhotoGridModel()
            view_widget.setModel(model)
            self._models[lane] = model
            self._views[lane] = view_widget
            self._tabs.addTab(view_widget, label)
        splitter.addWidget(centre)

        # Right provenance detail --------------------------------------------------
        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(8, 0, 0, 0)
        detail_title = QLabel("Timeline placement")
        detail_title.setObjectName("SectionHeader")
        detail_layout.addWidget(detail_title)
        self._detail = QLabel("Select a photograph to inspect why it appears in this lane.")
        self._detail.setWordWrap(True)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignTop)
        detail_layout.addWidget(self._detail, 1)
        self._story_btn = QPushButton("Story view…")
        self._story_btn.setVisible(False)
        self._story_btn.clicked.connect(self._open_event_story)
        detail_layout.addWidget(self._story_btn)
        self._event_btn = QPushButton("Name this cluster…")
        self._event_btn.setVisible(False)
        self._event_btn.clicked.connect(self._name_or_rename_event)
        detail_layout.addWidget(self._event_btn)
        self._open_btn = QPushButton("Open photo")
        self._open_btn.setEnabled(False)
        self._open_btn.clicked.connect(self._open_selected)
        detail_layout.addWidget(self._open_btn)
        splitter.addWidget(detail_panel)
        splitter.setSizes([175, 760, 245])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        root.addLayout(buttons)

        # Dedicated thumbnail worker. Image decode/cache work never blocks Qt.
        # All widgets referenced by tab-change handlers now exist, so it is
        # safe to enable tab-change callbacks.
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._thumb_worker = ThumbnailWorker(self._cache_dir, size=180, conn=self._conn)
        self._thumb_worker.ready.connect(self._on_thumbnail_ready, Qt.ConnectionType.QueuedConnection)
        self._registry.start_persistent(self._thumb_worker)
        for model in self._models.values():
            model.request_thumbnail.connect(
                self._thumb_worker.request, Qt.ConnectionType.QueuedConnection)

        self._populate_navigation()
        if self._nav.count():
            self._nav.setCurrentRow(0)
        else:
            self._refresh_visible()

    # --- navigation -------------------------------------------------------------
    def _populate_navigation(self) -> None:
        self._nav.clear()
        dated_count = sum(1 for i in self._view.items if i.start_date is not None and i.lane != "unplaced")
        all_item = QListWidgetItem(f"All dated  ({dated_count})")
        all_item.setData(Qt.ItemDataRole.UserRole, "all")
        self._nav.addItem(all_item)
        if self._scale == "cluster":
            named = {e.source_cluster_key: e for e in self._events if e.source_cluster_key}
            for cluster in self._clusters.clusters:
                context = len(cluster.context_file_ids)
                suffix = f" + {context} contextual" if context else ""
                event = named.get(cluster.key)
                prefix = f"{event.name} — " if event is not None else ""
                item = QListWidgetItem(f"{prefix}{cluster.label}{suffix}")
                item.setData(Qt.ItemDataRole.UserRole, cluster.key)
                item.setToolTip(cluster.reason)
                self._nav.addItem(item)
        elif self._scale == "event":
            for event in self._events:
                label = event.start_date if event.start_date == event.end_date else f"{event.start_date} → {event.end_date}"
                item = QListWidgetItem(f"{event.name}  ·  {label}  ({len(event.file_ids)})")
                item.setData(Qt.ItemDataRole.UserRole, event.id)
                item.setToolTip("Human-authored event identity with durable membership snapshot.")
                self._nav.addItem(item)
        else:
            for bucket in density_buckets(self._view, scale=self._scale):
                item = QListWidgetItem(f"{bucket.label}  ({bucket.count})")
                item.setData(Qt.ItemDataRole.UserRole, bucket.key)
                self._nav.addItem(item)
        if self._view.lanes["unplaced"].count:
            item = QListWidgetItem(f"Unplaced  ({self._view.lanes['unplaced'].count})")
            item.setData(Qt.ItemDataRole.UserRole, "unplaced")
            self._nav.addItem(item)
        self._update_density_hint()

    def _on_scale_changed(self, _index: int) -> None:
        self._scale = self._scale_combo.currentData() or "year"
        old_key = self._nav_key
        self._populate_navigation()
        target = 0
        for row in range(self._nav.count()):
            if self._nav.item(row).data(Qt.ItemDataRole.UserRole) == old_key:
                target = row
                break
        if self._nav.count():
            self._nav.setCurrentRow(target)

    def _on_nav_changed(self, current, _previous) -> None:
        if current is None:
            return
        self._nav_key = current.data(Qt.ItemDataRole.UserRole) or "all"
        self._lane_pages = {lane: 0 for lane, _ in _LANES}
        if self._nav_key == "unplaced":
            self._tabs.setCurrentIndex(3)
        elif self._tabs.currentIndex() == 3:
            self._tabs.setCurrentIndex(0)
        self._refresh_visible()
        self._update_density_hint()
        self._sync_event_action()

    def _on_tab_changed(self, _index: int) -> None:
        self._clear_detail()
        self._render_active_lane()
        self._update_scope_label()
        self._sync_event_action()

    def _active_lane(self) -> str:
        idx = max(0, self._tabs.currentIndex())
        return _LANES[idx][0]

    def _items_for_lane(self, lane: str):
        if self._nav_key == "unplaced":
            return tuple(i for i in self._view.items if i.lane == "unplaced") if lane == "unplaced" else ()
        if lane == "unplaced":
            return ()
        if self._nav_key == "all":
            return tuple(i for i in self._view.items if i.lane == lane and i.start_date is not None)
        cluster = self._cluster_by_key.get(self._nav_key)
        if cluster is not None:
            return items_for_cluster(self._view, cluster, lane=lane)
        event = self._event_by_id.get(self._nav_key)
        if event is not None:
            return items_for_event(self._view, event, lane=lane)
        return filter_for_bucket(self._view, bucket_key=self._nav_key, lane=lane)

    def _refresh_visible(self) -> None:
        # Keep the full filtered tuple only as lightweight TimelineItems.  The
        # catalogue/grid layer materialises at most one bounded page per lane.
        for idx, (lane, label) in enumerate(_LANES):
            items = self._items_for_lane(lane)
            self._visible_items[lane] = items
            page_no = self._lane_pages.get(lane, 0)
            pages = max(1, (len(items) + self._page_size - 1) // self._page_size)
            if page_no >= pages:
                page_no = pages - 1
                self._lane_pages[lane] = page_no
            page = page_items(items, page=page_no, page_size=self._page_size)
            ids = [i.file_id for i in page.items]
            grid_items = catalogue.grid_items_for_files(self._conn, ids)
            self._models[lane].set_items(grid_items)
            self._tabs.setTabText(idx, f"{label} ({len(items)})")
        self._clear_detail()
        self._update_scope_label()
        self._update_page_controls()

    def _render_active_lane(self) -> None:
        lane = self._active_lane()
        items = self._items_for_lane(lane)
        self._visible_items[lane] = items
        page_no = self._lane_pages.get(lane, 0)
        pages = max(1, (len(items) + self._page_size - 1) // self._page_size)
        if page_no >= pages:
            page_no = pages - 1
            self._lane_pages[lane] = page_no
        page = page_items(items, page=page_no, page_size=self._page_size)
        grid_items = catalogue.grid_items_for_files(self._conn, [i.file_id for i in page.items])
        self._models[lane].set_items(grid_items)
        self._update_page_controls()

    def _update_page_controls(self) -> None:
        lane = self._active_lane()
        items = self._visible_items.get(lane, ())
        page_no = self._lane_pages.get(lane, 0)
        page = page_items(items, page=page_no, page_size=self._page_size)
        self._page_label.setText(
            f"{page.start_index + 1 if page.total_items else 0}–{page.end_index} of {page.total_items} · "
            f"page {page.page + 1}/{page.total_pages}")
        self._prev_page.setEnabled(page.has_previous)
        self._next_page.setEnabled(page.has_next)
        if page.total_pages <= 1:
            self._scrubber.setValue(0)
            self._scrubber.setEnabled(False)
        else:
            self._scrubber.setEnabled(True)
            self._scrubber.blockSignals(True)
            self._scrubber.setValue(round(1000 * page.page / (page.total_pages - 1)))
            self._scrubber.blockSignals(False)

    def _page_previous(self) -> None:
        lane = self._active_lane()
        self._lane_pages[lane] = max(0, self._lane_pages.get(lane, 0) - 1)
        self._render_active_lane()

    def _page_next(self) -> None:
        lane = self._active_lane()
        items = self._visible_items.get(lane, ())
        pages = max(1, (len(items) + self._page_size - 1) // self._page_size)
        self._lane_pages[lane] = min(pages - 1, self._lane_pages.get(lane, 0) + 1)
        self._render_active_lane()

    def _scrub_to_position(self) -> None:
        lane = self._active_lane()
        items = self._visible_items.get(lane, ())
        fraction = self._scrubber.value() / 1000.0
        self._lane_pages[lane] = page_for_fraction(len(items), fraction, page_size=self._page_size)
        self._render_active_lane()

    def _update_density_hint(self) -> None:
        if self._nav_key in {"all", "unplaced"}:
            label = "entire dated collection" if self._nav_key == "all" else "unplaced collection"
            self._density_hint.setText(f"Scale: {self._scale} · {label}")
            return
        if self._scale == "cluster":
            cluster = self._cluster_by_key.get(self._nav_key)
            if cluster is None:
                self._density_hint.setText(f"Scale: clusters · {len(self._clusters.clusters)} detected")
            else:
                context = len(cluster.context_file_ids)
                self._density_hint.setText(
                    f"Provisional cluster · {cluster.authoritative_count} authoritative photo{'s' if cluster.authoritative_count != 1 else ''}"
                    + (f" · {context} contextual" if context else ""))
            self._sync_event_action()
            return
        if self._scale == "event":
            event = self._event_by_id.get(self._nav_key)
            if event is None:
                self._density_hint.setText(f"Scale: events · {len(self._events)} named")
            else:
                self._density_hint.setText(f"Human event · {event.name} · {len(event.file_ids)} member photo{'s' if len(event.file_ids) != 1 else ''}")
            self._sync_event_action()
            return
        buckets = {b.key: b for b in density_buckets(self._view, scale=self._scale)}
        bucket = buckets.get(self._nav_key)
        if bucket is None:
            self._density_hint.setText(f"Scale: {self._scale}")
        else:
            self._density_hint.setText(f"Scale: {self._scale} · density {bucket.count} photo{'s' if bucket.count != 1 else ''}")

    def _update_scope_label(self) -> None:
        lane = self._active_lane()
        visible = len(self._visible_items.get(lane, ()))
        cluster = self._cluster_by_key.get(self._nav_key)
        event = self._event_by_id.get(self._nav_key)
        scope = "All dated photos" if self._nav_key == "all" else (
            "Unplaced photos" if self._nav_key == "unplaced" else
            (f"Provisional cluster: {cluster.label}" if cluster is not None else
             (f"Event: {event.name}" if event is not None else self._nav_key)))
        self._scope_label.setText(
            f"{scope} · {lane.title()} lane · {visible} photo{'s' if visible != 1 else ''} · "
            f"showing at most {self._page_size} thumbnails at once")

    def _sync_event_action(self) -> None:
        cluster = self._cluster_by_key.get(self._nav_key) if self._scale == "cluster" else None
        event = self._event_by_id.get(self._nav_key) if self._scale == "event" else None
        if cluster is not None:
            existing = next((e for e in self._events if e.source_cluster_key == cluster.key), None)
            self._event_btn.setVisible(True)
            self._event_btn.setText("Edit event…" if existing else "Name this cluster…")
            self._event_btn.setProperty("event_id", existing.id if existing else None)
            self._story_btn.setVisible(existing is not None)
            self._story_btn.setProperty("event_id", existing.id if existing else None)
        elif event is not None:
            self._event_btn.setVisible(True)
            self._event_btn.setText("Edit event…")
            self._event_btn.setProperty("event_id", event.id)
            self._story_btn.setVisible(True)
            self._story_btn.setProperty("event_id", event.id)
        else:
            self._event_btn.setVisible(False)
            self._event_btn.setProperty("event_id", None)
            self._story_btn.setVisible(False)
            self._story_btn.setProperty("event_id", None)

    def _open_event_story(self) -> None:
        event_id = self._story_btn.property("event_id")
        if not event_id:
            return
        from ppa.ui.event_story_dialog import EventStoryDialog
        try:
            dialog = EventStoryDialog(
                self._conn, self._view, str(event_id), self, cache_dir=self._cache_dir / "events")
        except ValueError as exc:
            QMessageBox.warning(self, "Event story", str(exc))
            return
        dialog.show()

    def _reload_events(self) -> None:
        self._events = list_events(self._conn, library_id=self._view.scope.library_id)
        self._event_by_id = {e.id: e for e in self._events}

    def _name_or_rename_event(self) -> None:
        event_id = self._event_btn.property("event_id")
        if event_id:
            current = self._event_by_id.get(str(event_id)) or next((e for e in self._events if e.id == str(event_id)), None)
            if current is None:
                QMessageBox.warning(self, "Event", "That event is no longer available.")
                return
            dlg = EventEditorDialog(self._conn, current, self._view, self)
            dlg.exec()
        else:
            cluster = self._cluster_by_key.get(self._nav_key)
            if cluster is None:
                return
            name, ok = QInputDialog.getText(
                self, "Name chronological cluster",
                "Human event name (the detected cluster itself remains provisional):")
            if not ok:
                return
            try:
                create_event_from_cluster(
                    self._conn, library_id=self._view.scope.library_id, cluster=cluster, name=name)
            except ValueError as exc:
                QMessageBox.warning(self, "Event", str(exc))
                return
        old_key = self._nav_key
        self._reload_events()
        self._populate_navigation()
        for row in range(self._nav.count()):
            if self._nav.item(row).data(Qt.ItemDataRole.UserRole) == old_key:
                self._nav.setCurrentRow(row)
                break
        self._sync_event_action()

    # --- thumbnail / selection --------------------------------------------------
    @Slot(str, object)
    def _on_thumbnail_ready(self, file_id: str, image) -> None:
        pm = QPixmap.fromImage(image)
        for model in self._models.values():
            model.set_thumbnail(file_id, pm)

    def _timeline_item(self, file_id: str):
        for item in self._view.items:
            if item.file_id == file_id:
                return item
        return None

    def _show_index_detail(self, index: QModelIndex) -> None:
        fid = index.data(FILE_ID_ROLE)
        item = self._timeline_item(fid) if fid else None
        self._selected_item = item
        self._open_btn.setEnabled(item is not None)
        if item is None:
            self._clear_detail()
            return
        date_text = "Unplaced"
        if item.start_date:
            date_text = item.start_date if not item.end_date else f"{item.start_date} → {item.end_date}"
        stale = []
        if item.content_stale:
            stale.append("photo changed")
        if item.evidence_stale:
            stale.append("evidence changed")
        freshness = f"\nFreshness: STALE — {', '.join(stale)}" if stale else "\nFreshness: current"
        self._detail.setText(
            f"{item.filename}\n\n"
            f"Date: {date_text}\n"
            f"Lane: {item.lane}\n"
            f"Source: {item.source.replace('_', ' ')}\n"
            f"Reliability: {item.reliability}\n"
            f"Confidence: {item.confidence or '—'}\n"
            f"Method: {item.method or '—'}"
            f"{freshness}\n\n"
            f"Why: {item.reason}")

    def _clear_detail(self) -> None:
        self._selected_item = None
        self._open_btn.setEnabled(False)
        self._detail.setText("Select a photograph to inspect why it appears in this lane.")

    # --- preview ---------------------------------------------------------------
    def _open_index(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        fid = index.data(FILE_ID_ROLE)
        if not fid:
            return
        self._open_file_id(fid)

    def _open_selected(self) -> None:
        if self._selected_item is not None:
            self._open_file_id(self._selected_item.file_id)

    def _open_file_id(self, fid: str) -> None:
        lane = self._timeline_item(fid).lane if self._timeline_item(fid) else self._active_lane()
        items = self._visible_items.get(lane, ())
        page_no = self._lane_pages.get(lane, 0)
        page = page_items(items, page=page_no, page_size=self._page_size)
        ids = [i.file_id for i in page.items]
        if fid not in ids:
            ids = [fid]
        model = PhotoGridModel()
        grid_items = catalogue.grid_items_for_files(self._conn, ids)
        model.set_items(grid_items)
        index = next((n for n, x in enumerate(grid_items) if x.file_id == fid), 0)
        from ppa.ui.preview_dialog import PreviewDialog
        dialog = PreviewDialog(self._conn, model, index, self,
                               window_title=f"Timeline — {lane.title()}")
        dialog._timeline_model = model
        dialog.show()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._registry.shutdown()
        super().closeEvent(event)
