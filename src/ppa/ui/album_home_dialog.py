"""Phase 9.4 — visual Album library landing page."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout,
)

from ppa.album_home import AlbumHomeCard, AlbumHomeView
from ppa.timeline_scale import page_items
from ppa.ui.workers import ThumbnailWorker, WorkerRegistry

_ALBUM_ID_ROLE = Qt.ItemDataRole.UserRole
_COVER_FILE_ROLE = Qt.ItemDataRole.UserRole + 1


class AlbumHomeDialog(QDialog):
    """Read-only card index for one Library's Albums."""
    _PAGE_SIZE = 30

    def __init__(self, conn, home: AlbumHomeView, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._home = home
        self._cache_dir = Path(cache_dir or Path.home()/'.cache'/'personal-photo-archive'/'album-home')
        self._registry = WorkerRegistry()
        self._page = 0
        self._visible: tuple[AlbumHomeCard, ...] = home.cards
        self._items_by_cover: dict[str, list[QListWidgetItem]] = {}

        self.setWindowTitle('Albums')
        self.resize(1120, 780)
        root = QVBoxLayout(self)
        title = QLabel('Albums'); title.setObjectName('SectionHeader'); root.addWidget(title)
        subtitle = QLabel('Human-curated Albums. Covers and ordering are presentation only and never chronology evidence.')
        subtitle.setWordWrap(True); subtitle.setObjectName('FieldKey'); root.addWidget(subtitle)

        row = QHBoxLayout(); row.addWidget(QLabel('Search:'))
        self._search = QLineEdit(); self._search.setPlaceholderText('Search album names and descriptions…')
        self._search.setClearButtonEnabled(True); self._search.textChanged.connect(self._apply_filter); row.addWidget(self._search, 1)
        self._status = QLabel(); self._status.setObjectName('FieldKey'); row.addWidget(self._status)
        root.addLayout(row)

        self._list = QListWidget()
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setIconSize(QSize(190, 150)); self._list.setGridSize(QSize(245, 245)); self._list.setSpacing(8)
        self._list.itemDoubleClicked.connect(self._open_item)
        self._list.currentItemChanged.connect(self._selection_changed)
        root.addWidget(self._list, 1)

        nav = QHBoxLayout()
        self._prev = QPushButton('◀ Previous'); self._prev.clicked.connect(self._previous_page)
        self._page_label = QLabel(); self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next = QPushButton('Next ▶'); self._next.clicked.connect(self._next_page)
        nav.addWidget(self._prev); nav.addWidget(self._page_label, 1); nav.addWidget(self._next); root.addLayout(nav)

        bottom = QHBoxLayout(); bottom.addStretch(1)
        self._open = QPushButton('Open album'); self._open.clicked.connect(self._open_selected); bottom.addWidget(self._open)
        close = QPushButton('Close'); close.clicked.connect(self.close); bottom.addWidget(close); root.addLayout(bottom)

        self._thumb_worker = ThumbnailWorker(self._cache_dir, size=220)
        self._thumb_worker.ready.connect(self._thumbnail_ready, Qt.ConnectionType.QueuedConnection)
        self._registry.start_persistent(self._thumb_worker)
        self._render_page()

    def _apply_filter(self, text: str) -> None:
        self._visible = self._home.filtered(text)
        self._page = 0
        self._render_page()

    def _render_page(self) -> None:
        page = page_items(self._visible, page=self._page, page_size=self._PAGE_SIZE)
        self._page = page.page
        self._list.clear(); self._items_by_cover.clear()
        for card in page.items:
            flags = []
            if card.has_custom_cover: flags.append('custom cover')
            if card.has_custom_order: flags.append('custom order')
            if card.missing_only_count: flags.append(f'{card.missing_only_count} missing-only')
            detail = f"{card.photo_count} photo{'s' if card.photo_count != 1 else ''}"
            if flags: detail += ' · ' + ' · '.join(flags)
            snippet = ' '.join((card.description or '').split())
            if len(snippet) > 100: snippet = snippet[:99].rstrip() + '…'
            text = f"{card.name}\n{detail}" + (f"\n{snippet}" if snippet else '')
            item = QListWidgetItem(text)
            item.setData(_ALBUM_ID_ROLE, card.album_id)
            item.setData(_COVER_FILE_ROLE, card.cover_file_id)
            item.setToolTip(f"Cover rule: {card.cover_rule.replace('_',' ')}")
            self._list.addItem(item)
            if card.cover_file_id and card.cover_path:
                self._items_by_cover.setdefault(card.cover_file_id, []).append(item)
                self._thumb_worker.request(card.cover_file_id, card.cover_path, card.cover_sha256 or '')
        self._status.setText(f"{len(self._visible)} of {len(self._home.cards)} albums")
        self._page_label.setText(
            f"{page.start_index + 1 if page.total_items else 0}–{page.end_index} of {page.total_items} · "
            f"page {page.page + 1}/{page.total_pages}")
        self._prev.setEnabled(page.has_previous); self._next.setEnabled(page.has_next)
        self._open.setEnabled(self._list.currentItem() is not None)

    @Slot(str, object)
    def _thumbnail_ready(self, file_id: str, image) -> None:
        icon = QIcon(QPixmap.fromImage(image))
        for item in self._items_by_cover.get(file_id, ()): item.setIcon(icon)

    def _selection_changed(self, current, _previous) -> None:
        self._open.setEnabled(current is not None)

    def _previous_page(self) -> None:
        self._page = max(0, self._page-1); self._render_page()

    def _next_page(self) -> None:
        page = page_items(self._visible, page=self._page, page_size=self._PAGE_SIZE)
        if page.has_next: self._page += 1; self._render_page()

    def _open_item(self, item: QListWidgetItem) -> None:
        aid = item.data(_ALBUM_ID_ROLE)
        if aid: self._open_album(str(aid))

    def _open_selected(self) -> None:
        item = self._list.currentItem()
        if item is not None: self._open_item(item)

    def _open_album(self, album_id: str) -> None:
        from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog
        dialog = OrganizationBrowseDialog(self._conn, 'album', album_id, self,
                                           cache_dir=self._cache_dir/'browse')
        dialog.show(); self._album_dialog = dialog

    def closeEvent(self, event) -> None:  # noqa: N802
        self._registry.shutdown(); super().closeEvent(event)
