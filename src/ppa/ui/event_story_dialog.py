"""Phase 8.7 — album-style read-only Event Story browser."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QListView, QPushButton,
    QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from ppa import catalogue
from ppa.event_navigation import build_event_browse_index
from ppa.event_activity import get_event_activity, record_event_view, set_event_favorite
from ppa.event_story import EventStoryPhoto, EventStoryView, build_event_story
from ppa.timeline_scale import DEFAULT_PAGE_SIZE, page_items
from ppa.ui.delegate import PhotoTileDelegate
from ppa.ui.models import FILE_ID_ROLE, PhotoGridModel
from ppa.ui.workers import ThumbnailWorker, WorkerRegistry


class EventStoryDialog(QDialog):
    """Read-only presentation of one durable human Event.

    Story/context is deliberately prominent; chronology provenance remains
    inspectable but never hidden or rewritten.  Thumbnail loading is bounded
    and asynchronous.
    """

    def __init__(self, conn, timeline_view, event_id: str, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._timeline_view = timeline_view
        self._story: EventStoryView = build_event_story(conn, timeline_view, event_id)
        # Opening a Story records navigation history only. It is intentionally
        # outside chronology/Event evidence and can never influence placement.
        self._activity = record_event_view(conn, event_id)
        self._browse = build_event_browse_index(conn, library_id=self._story.event.library_id)
        self._browse_card = self._browse.card(event_id)
        self._cache_dir = Path(cache_dir or Path.home() / ".cache" / "personal-photo-archive" / "event-story")
        self._registry = WorkerRegistry()
        self._model = PhotoGridModel()
        self._page_size = DEFAULT_PAGE_SIZE
        self._page = 0
        self._selected: EventStoryPhoto | None = None
        self._by_id = {p.file_id: p for p in self._story.photos}

        self.setWindowTitle(self._story.event.name)
        self.resize(1120, 780)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Event-to-event reading controls are pure navigation over durable Event
        # identity. They never change membership, story context, or chronology.
        event_nav = QHBoxLayout()
        self._previous_event = QPushButton("◀ Previous event")
        self._previous_event.setEnabled(self._browse_card.previous_event_id is not None)
        self._previous_event.clicked.connect(
            lambda: self._navigate_event(self._browse_card.previous_event_id))
        event_nav.addWidget(self._previous_event)
        self._event_position = QLabel(
            f"{self._browse_card.year} · Event {self._browse_card.position + 1} of {len(self._browse.cards)}")
        self._event_position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._event_position.setObjectName("FieldKey")
        event_nav.addWidget(self._event_position, 1)
        self._favorite = QPushButton("★ Favourite" if self._activity.favorite else "☆ Favourite")
        self._favorite.setCheckable(True); self._favorite.setChecked(self._activity.favorite)
        self._favorite.clicked.connect(self._toggle_favorite)
        event_nav.addWidget(self._favorite)
        self._next_event = QPushButton("Next event ▶")
        self._next_event.setEnabled(self._browse_card.next_event_id is not None)
        self._next_event.clicked.connect(
            lambda: self._navigate_event(self._browse_card.next_event_id))
        event_nav.addWidget(self._next_event)
        root.addLayout(event_nav)

        # Human memory is primary in this view.
        title = QLabel(self._story.event.name)
        title.setObjectName("SectionHeader")
        root.addWidget(title)

        span = self._story.event.start_date
        if self._story.event.end_date != self._story.event.start_date:
            span = f"{span} → {self._story.event.end_date}"
        ctx = self._story.context
        meta_parts = [span]
        if ctx.occasion_text:
            meta_parts.append(ctx.occasion_text)
        if ctx.place_text:
            meta_parts.append(ctx.place_text)
        self._meta = QLabel(" · ".join(meta_parts))
        self._meta.setWordWrap(True)
        self._meta.setObjectName("FieldKey")
        root.addWidget(self._meta)

        if ctx.people_text:
            people = QLabel(f"People: {ctx.people_text}")
            people.setWordWrap(True)
            root.addWidget(people)
        if ctx.description:
            description = QLabel(ctx.description)
            description.setWordWrap(True)
            root.addWidget(description)
        if ctx.story_text:
            story_text = QTextEdit()
            story_text.setReadOnly(True)
            story_text.setPlainText(ctx.story_text)
            story_text.setMaximumHeight(130)
            root.addWidget(story_text)

        counts = self._story.lane_counts
        coverage = QLabel(
            f"{len(self._story.photos)} visible event members · "
            f"{counts['placed']} placed · {counts['range']} ranges · "
            f"{counts['tentative']} tentative · {counts['unplaced']} unplaced")
        coverage.setObjectName("FieldKey")
        coverage.setWordWrap(True)
        root.addWidget(coverage)

        body = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(body, 1)

        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        self._grid = QListView()
        self._grid.setViewMode(QListView.ViewMode.IconMode)
        self._grid.setResizeMode(QListView.ResizeMode.Adjust)
        self._grid.setMovement(QListView.Movement.Static)
        self._grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._grid.setUniformItemSizes(True)
        self._grid.setSpacing(8)
        self._grid.setIconSize(QSize(170, 170))
        self._grid.setGridSize(QSize(200, 225))
        self._grid.setItemDelegate(PhotoTileDelegate(self._grid))
        self._grid.setModel(self._model)
        self._grid.clicked.connect(self._show_detail)
        self._grid.doubleClicked.connect(self._open_index)
        cl.addWidget(self._grid, 1)

        page_row = QHBoxLayout()
        self._prev = QPushButton("◀ Previous")
        self._prev.clicked.connect(self._page_previous)
        self._page_label = QLabel("")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next = QPushButton("Next ▶")
        self._next.clicked.connect(self._page_next)
        page_row.addWidget(self._prev)
        page_row.addWidget(self._page_label, 1)
        page_row.addWidget(self._next)
        cl.addLayout(page_row)
        body.addWidget(centre)

        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 0, 0, 0)
        heading = QLabel("Photo context")
        heading.setObjectName("SectionHeader")
        sl.addWidget(heading)
        self._detail = QLabel("Select a photo. Story membership and chronology provenance are shown separately.")
        self._detail.setWordWrap(True)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignTop)
        sl.addWidget(self._detail, 1)
        self._provenance = QLabel("")
        self._provenance.setWordWrap(True)
        self._provenance.setVisible(False)
        sl.addWidget(self._provenance)
        self._toggle = QPushButton("Show provenance")
        self._toggle.setEnabled(False)
        self._toggle.clicked.connect(self._toggle_provenance)
        sl.addWidget(self._toggle)
        self._open = QPushButton("Open photo")
        self._open.setEnabled(False)
        self._open.clicked.connect(self._open_selected)
        sl.addWidget(self._open)
        body.addWidget(side)
        body.setSizes([820, 280])

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        root.addLayout(bottom)

        self._thumb_worker = ThumbnailWorker(self._cache_dir, size=200)
        self._thumb_worker.ready.connect(self._thumbnail_ready, Qt.ConnectionType.QueuedConnection)
        self._registry.start_persistent(self._thumb_worker)
        self._model.request_thumbnail.connect(
            self._thumb_worker.request, Qt.ConnectionType.QueuedConnection)
        self._render_page()

    def _render_page(self) -> None:
        page = page_items(self._story.photos, page=self._page, page_size=self._page_size)
        self._page = page.page
        ids = [p.file_id for p in page.items]
        self._model.set_items(catalogue.grid_items_for_files(self._conn, ids))
        self._page_label.setText(
            f"{page.start_index + 1 if page.total_items else 0}–{page.end_index} of {page.total_items} · "
            f"page {page.page + 1}/{page.total_pages}")
        self._prev.setEnabled(page.has_previous)
        self._next.setEnabled(page.has_next)
        self._selected = None
        self._open.setEnabled(False)
        self._toggle.setEnabled(False)
        self._provenance.setVisible(False)
        self._toggle.setText("Show provenance")
        self._detail.setText("Select a photo. Story membership and chronology provenance are shown separately.")

    def _page_previous(self) -> None:
        self._page = max(0, self._page - 1)
        self._render_page()

    def _page_next(self) -> None:
        page = page_items(self._story.photos, page=self._page, page_size=self._page_size)
        if page.has_next:
            self._page += 1
            self._render_page()

    @Slot(str, object)
    def _thumbnail_ready(self, file_id: str, image) -> None:
        self._model.set_thumbnail(file_id, QPixmap.fromImage(image))

    def _story_photo(self, index: QModelIndex) -> EventStoryPhoto | None:
        fid = index.data(FILE_ID_ROLE) if index.isValid() else None
        return self._by_id.get(str(fid)) if fid else None

    def _show_detail(self, index: QModelIndex) -> None:
        photo = self._story_photo(index)
        self._selected = photo
        self._open.setEnabled(photo is not None)
        self._toggle.setEnabled(photo is not None)
        self._provenance.setVisible(False)
        self._toggle.setText("Show provenance")
        if photo is None:
            return
        when = "Date unresolved"
        if photo.start_date:
            when = photo.start_date if not photo.end_date else f"{photo.start_date} → {photo.end_date}"
        role = "Original event seed" if photo.member_role == "authoritative_seed" else "Human-added event member"
        self._detail.setText(
            f"{photo.filename}\n\n"
            f"{when}\n"
            f"{role}\n"
            f"Current chronology lane: {photo.lane.title()}")
        stale = []
        if photo.content_stale:
            stale.append("photo changed")
        if photo.evidence_stale:
            stale.append("evidence changed")
        freshness = f"STALE — {', '.join(stale)}" if stale else "current"
        self._provenance.setText(
            f"Chronology provenance\n"
            f"Source: {photo.source.replace('_', ' ')}\n"
            f"Reliability: {photo.reliability}\n"
            f"Confidence: {photo.confidence or '—'}\n"
            f"Method: {photo.method or '—'}\n"
            f"Freshness: {freshness}\n\n"
            f"Why: {photo.reason}")

    def _toggle_provenance(self) -> None:
        if self._selected is None:
            return
        visible = not self._provenance.isVisible()
        self._provenance.setVisible(visible)
        self._toggle.setText("Hide provenance" if visible else "Show provenance")

    def _open_index(self, index: QModelIndex) -> None:
        photo = self._story_photo(index)
        if photo is not None:
            self._open_file(photo.file_id)

    def _open_selected(self) -> None:
        if self._selected is not None:
            self._open_file(self._selected.file_id)

    def _open_file(self, file_id: str) -> None:
        page = page_items(self._story.photos, page=self._page, page_size=self._page_size)
        ids = [p.file_id for p in page.items]
        if file_id not in ids:
            ids = [file_id]
        model = PhotoGridModel()
        grid_items = catalogue.grid_items_for_files(self._conn, ids)
        model.set_items(grid_items)
        index = next((n for n, item in enumerate(grid_items) if item.file_id == file_id), 0)
        from ppa.ui.preview_dialog import PreviewDialog
        dialog = PreviewDialog(self._conn, model, index, self, window_title=self._story.event.name)
        dialog._event_story_model = model
        dialog.show()



    def _toggle_favorite(self) -> None:
        self._activity = set_event_favorite(self._conn, self._story.event.id, self._favorite.isChecked())
        self._favorite.setText("★ Favourite" if self._activity.favorite else "☆ Favourite")

    def _navigate_event(self, event_id: str | None) -> None:
        if not event_id:
            return
        # Open the next story against the same already-authorised Timeline
        # projection, then retire this dialog. This keeps navigation read-only
        # and avoids recomputing chronology between adjacent human Events.
        dialog = EventStoryDialog(
            self._conn, self._timeline_view, event_id, self.parentWidget(), cache_dir=self._cache_dir)
        dialog.show()
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._registry.shutdown()
        super().closeEvent(event)
