"""Phase 9.2 — paged visual browser for one Album or Tag."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QLineEdit, QListView,
    QPushButton, QVBoxLayout,
)

from ppa.catalogue import GridItem
from ppa.organization_browse import OrganizationBrowseView, build_organization_browse
from ppa.timeline_scale import DEFAULT_PAGE_SIZE, page_items
from ppa.ui.delegate import PhotoTileDelegate
from ppa.ui.models import FILE_ID_ROLE, PhotoGridModel
from ppa.ui.workers import ThumbnailWorker, WorkerRegistry


class OrganizationBrowseDialog(QDialog):
    """Read-only Album/Tag membership browser.

    A logical Photo is rendered once using the deterministic representative
    File chosen by the pure Phase-9.2 projection.
    """

    def __init__(self, conn, object_kind: str, object_id: str, parent=None, *,
                 cache_dir: Path | None = None, view: OrganizationBrowseView | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._view: OrganizationBrowseView = view or build_organization_browse(
            conn, object_kind=object_kind, object_id=object_id)
        self._cache_dir = Path(cache_dir or Path.home()/'.cache'/'personal-photo-archive'/'organization')
        self._registry = WorkerRegistry()
        self._model = PhotoGridModel()
        self._page_size = DEFAULT_PAGE_SIZE
        self._page = 0
        self._filtered = self._view.items

        kind_label = 'Album' if object_kind == 'album' else ('Tag' if object_kind == 'tag' else 'Photos')
        self.setWindowTitle(f"{kind_label}: {self._view.name}")
        self.resize(1080, 760)
        root = QVBoxLayout(self)

        title = QLabel(self._view.name); title.setObjectName('SectionHeader'); root.addWidget(title)
        if self._view.description:
            desc = QLabel(self._view.description); desc.setWordWrap(True); root.addWidget(desc)
        self._summary = QLabel(); self._summary.setObjectName('FieldKey'); root.addWidget(self._summary)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel('Filter:'))
        self._search = QLineEdit(); self._search.setPlaceholderText('Filter by filename…')
        self._search.textChanged.connect(self._apply_filter)
        search_row.addWidget(self._search, 1)
        clear = QPushButton('Clear'); clear.clicked.connect(self._search.clear); search_row.addWidget(clear)
        root.addLayout(search_row)

        self._grid = QListView()
        self._grid.setViewMode(QListView.ViewMode.IconMode)
        self._grid.setResizeMode(QListView.ResizeMode.Adjust)
        self._grid.setMovement(QListView.Movement.Static)
        self._grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._grid.setUniformItemSizes(True); self._grid.setSpacing(8)
        self._grid.setIconSize(QSize(170,170)); self._grid.setGridSize(QSize(200,225))
        self._grid.setItemDelegate(PhotoTileDelegate(self._grid)); self._grid.setModel(self._model)
        self._grid.doubleClicked.connect(self._open_index)
        root.addWidget(self._grid, 1)

        nav = QHBoxLayout()
        self._prev = QPushButton('◀ Previous'); self._prev.clicked.connect(self._previous_page)
        self._page_label = QLabel(); self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next = QPushButton('Next ▶'); self._next.clicked.connect(self._next_page)
        nav.addWidget(self._prev); nav.addWidget(self._page_label,1); nav.addWidget(self._next)
        root.addLayout(nav)

        bottom = QHBoxLayout(); bottom.addStretch(1)
        self._open = QPushButton('Open photo'); self._open.clicked.connect(self._open_selected); bottom.addWidget(self._open)
        close = QPushButton('Close'); close.clicked.connect(self.close); bottom.addWidget(close)
        root.addLayout(bottom)

        self._thumb_worker = ThumbnailWorker(self._cache_dir, size=200, conn=self._conn)
        self._thumb_worker.ready.connect(self._thumbnail_ready, Qt.ConnectionType.QueuedConnection)
        self._registry.start_persistent(self._thumb_worker)
        self._model.request_thumbnail.connect(self._thumb_worker.request, Qt.ConnectionType.QueuedConnection)
        self._grid.selectionModel().selectionChanged.connect(lambda *_: self._update_open())
        self._render_page()

    def _grid_item(self, item, health_status: str = "unknown") -> GridItem:
        return GridItem(item.file_id, item.photo_id, item.filename, item.path,
                        item.sha256, item.status, item.width_px, item.height_px,
                        item.size_bytes, item.copy_count, health_status)

    def _apply_filter(self, text: str) -> None:
        self._filtered = self._view.filtered(text)
        self._page = 0
        self._render_page()

    def _render_page(self) -> None:
        page = page_items(self._filtered, page=self._page, page_size=self._page_size)
        self._page = page.page
        ids = [i.file_id for i in page.items]
        health_by_id = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            health_by_id = {r["id"]: r["health_status"] for r in self._conn.execute(
                f"SELECT id, health_status FROM files WHERE id IN ({marks})", ids
            ).fetchall()}
        self._model.set_items([
            self._grid_item(i, health_by_id.get(i.file_id, "unknown")) for i in page.items
        ])
        self._summary.setText(
            f"{len(self._filtered)} of {self._view.total_members} logical photos · "
            f"{self._view.present_members} present · {self._view.missing_only_members} missing-only")
        self._page_label.setText(
            f"{page.start_index + 1 if page.total_items else 0}–{page.end_index} of {page.total_items} · "
            f"page {page.page + 1}/{page.total_pages}")
        self._prev.setEnabled(page.has_previous); self._next.setEnabled(page.has_next)
        self._update_open()

    def _previous_page(self) -> None:
        self._page = max(0, self._page-1); self._render_page()

    def _next_page(self) -> None:
        page = page_items(self._filtered, page=self._page, page_size=self._page_size)
        if page.has_next:
            self._page += 1; self._render_page()

    def _update_open(self) -> None:
        self._open.setEnabled(bool(self._grid.selectionModel().selectedIndexes()))

    @Slot(str, object)
    def _thumbnail_ready(self, file_id: str, image) -> None:
        self._model.set_thumbnail(file_id, QPixmap.fromImage(image))

    def _open_index(self, index: QModelIndex) -> None:
        if index.isValid():
            self._show_preview(index.row())

    def _open_selected(self) -> None:
        indexes = self._grid.selectionModel().selectedIndexes()
        if indexes: self._show_preview(indexes[0].row())

    def _show_preview(self, row: int) -> None:
        if self._model.rowCount() == 0: return
        from ppa.ui.preview_dialog import PreviewDialog
        dialog = PreviewDialog(self._conn, self._model, row, self,
                                window_title=self._view.name)
        dialog.show()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._registry.shutdown()
        super().closeEvent(event)
