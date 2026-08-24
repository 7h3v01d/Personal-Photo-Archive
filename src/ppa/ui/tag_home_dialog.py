"""Phase 9.5 — visual Tag Home and explicit intersection launcher."""
from __future__ import annotations
from pathlib import Path
from PySide6.QtCore import QSize, Qt, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout
from ppa.tag_home import TagHomeCard, TagHomeView, build_tag_intersection_view
from ppa.timeline_scale import page_items
from ppa.ui.workers import ThumbnailWorker, WorkerRegistry

_TAG_ID_ROLE = Qt.ItemDataRole.UserRole

class TagHomeDialog(QDialog):
    _PAGE_SIZE = 30
    def __init__(self, conn, home: TagHomeView, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent); self._conn=conn; self._home=home
        self._cache_dir=Path(cache_dir or Path.home()/'.cache'/'personal-photo-archive'/'tag-home')
        self._registry=WorkerRegistry(); self._page=0; self._visible=home.cards; self._items_by_cover={}
        self.setWindowTitle('Tags'); self.resize(1120,780)
        root=QVBoxLayout(self); title=QLabel('Tags'); title.setObjectName('SectionHeader'); root.addWidget(title)
        sub=QLabel('Human-applied Tags. Select two or more Tags to browse their explicit intersection.'); sub.setWordWrap(True); sub.setObjectName('FieldKey'); root.addWidget(sub)
        row=QHBoxLayout(); row.addWidget(QLabel('Search:')); self._search=QLineEdit(); self._search.setPlaceholderText('Search tag names…'); self._search.setClearButtonEnabled(True); self._search.textChanged.connect(self._apply_filter); row.addWidget(self._search,1); self._status=QLabel(); row.addWidget(self._status); root.addLayout(row)
        self._list=QListWidget(); self._list.setViewMode(QListWidget.ViewMode.IconMode); self._list.setResizeMode(QListWidget.ResizeMode.Adjust); self._list.setMovement(QListWidget.Movement.Static); self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); self._list.setIconSize(QSize(180,140)); self._list.setGridSize(QSize(225,215)); self._list.setSpacing(8); self._list.itemDoubleClicked.connect(self._open_single_item); self._list.itemSelectionChanged.connect(self._selection_changed); root.addWidget(self._list,1)
        nav=QHBoxLayout(); self._prev=QPushButton('◀ Previous'); self._prev.clicked.connect(self._previous_page); self._page_label=QLabel(); self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self._next=QPushButton('Next ▶'); self._next.clicked.connect(self._next_page); nav.addWidget(self._prev); nav.addWidget(self._page_label,1); nav.addWidget(self._next); root.addLayout(nav)
        bottom=QHBoxLayout(); bottom.addStretch(1); self._open=QPushButton('Browse tag'); self._open.clicked.connect(self._open_selected); self._intersection=QPushButton('Browse intersection'); self._intersection.clicked.connect(self._open_intersection); bottom.addWidget(self._open); bottom.addWidget(self._intersection); close=QPushButton('Close'); close.clicked.connect(self.close); bottom.addWidget(close); root.addLayout(bottom)
        self._thumb_worker=ThumbnailWorker(self._cache_dir,size=210); self._thumb_worker.ready.connect(self._thumbnail_ready,Qt.ConnectionType.QueuedConnection); self._registry.start_persistent(self._thumb_worker); self._render_page()
    def _apply_filter(self,text): self._visible=self._home.filtered(text); self._page=0; self._render_page()
    def _render_page(self):
        page=page_items(self._visible,page=self._page,page_size=self._PAGE_SIZE); self._page=page.page; self._list.clear(); self._items_by_cover.clear()
        for card in page.items:
            detail=f"{card.photo_count} photo{'s' if card.photo_count!=1 else ''}" + (f" · {card.missing_only_count} missing-only" if card.missing_only_count else '')
            item=QListWidgetItem(f"{card.name}\n{detail}"); item.setData(_TAG_ID_ROLE,card.tag_id); self._list.addItem(item)
            if card.cover_file_id and card.cover_path:
                self._items_by_cover.setdefault(card.cover_file_id,[]).append(item); self._thumb_worker.request(card.cover_file_id,card.cover_path,card.cover_sha256 or '')
        self._status.setText(f"{len(self._visible)} of {len(self._home.cards)} tags"); self._page_label.setText(f"{page.start_index+1 if page.total_items else 0}–{page.end_index} of {page.total_items} · page {page.page+1}/{page.total_pages}"); self._prev.setEnabled(page.has_previous); self._next.setEnabled(page.has_next); self._selection_changed()
    @Slot(str,object)
    def _thumbnail_ready(self,file_id,image):
        icon=QIcon(QPixmap.fromImage(image))
        for item in self._items_by_cover.get(file_id,()): item.setIcon(icon)
    def _selection_changed(self):
        n=len(self._list.selectedItems()); self._open.setEnabled(n==1); self._intersection.setEnabled(n>=2)
    def _selected_ids(self): return tuple(str(i.data(_TAG_ID_ROLE)) for i in self._list.selectedItems() if i.data(_TAG_ID_ROLE))
    def _open_single_item(self,item): self._open_tag(str(item.data(_TAG_ID_ROLE)))
    def _open_selected(self):
        ids=self._selected_ids()
        if len(ids)==1: self._open_tag(ids[0])
    def _open_tag(self,tag_id):
        from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog
        d=OrganizationBrowseDialog(self._conn,'tag',tag_id,self,cache_dir=self._cache_dir/'browse'); d.show(); self._browse_dialog=d
    def _open_intersection(self):
        ids=self._selected_ids()
        if len(ids)<2: return
        from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog
        view=build_tag_intersection_view(self._conn,library_id=self._home.library_id,tag_ids=ids)
        d=OrganizationBrowseDialog(self._conn,'tag_intersection',view.object_id,self,cache_dir=self._cache_dir/'intersections',view=view); d.show(); self._browse_dialog=d
    def _previous_page(self): self._page=max(0,self._page-1); self._render_page()
    def _next_page(self):
        page=page_items(self._visible,page=self._page,page_size=self._PAGE_SIZE)
        if page.has_next: self._page+=1; self._render_page()
    def closeEvent(self,event): self._registry.shutdown(); super().closeEvent(event)
