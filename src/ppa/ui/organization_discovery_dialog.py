"""Phase 9.6 — unified Album + Tag discovery surface."""
from __future__ import annotations
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QProgressDialog, QPushButton, QSplitter, QVBoxLayout,
    QWidget,
)

from ppa.ui.workers import OrganizationDiscoveryRunWorker, WorkerRegistry

_ID_ROLE = Qt.ItemDataRole.UserRole


class OrganizationDiscoveryDialog(QDialog):
    def __init__(self, db_path: Path, album_home, tag_home, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._db_path=Path(db_path); self._albums=album_home; self._tags=tag_home
        if album_home.library_id != tag_home.library_id:
            raise ValueError('Album and Tag discovery homes must belong to the same Library')
        self._library_id=album_home.library_id
        self._cache_dir=Path(cache_dir or Path.home()/'.cache'/'personal-photo-archive'/'organization-discovery')
        self._registry=WorkerRegistry(); self._progress=None; self._browse_dialog=None
        self.setWindowTitle('Organisational Discovery'); self.resize(980,700)
        root=QVBoxLayout(self)
        title=QLabel('Organisational Discovery'); title.setObjectName('SectionHeader'); root.addWidget(title)
        sub=QLabel('Select any Albums and Tags. Results are the exact intersection of their explicit logical-Photo memberships.')
        sub.setWordWrap(True); sub.setObjectName('FieldKey'); root.addWidget(sub)
        savedrow=QHBoxLayout(); savedrow.addWidget(QLabel('Saved views:'))
        self._saved=QComboBox(); self._saved.currentIndexChanged.connect(self._apply_saved_view); savedrow.addWidget(self._saved,1)
        self._save_view=QPushButton('Save…'); self._save_view.clicked.connect(self._save_current_view); savedrow.addWidget(self._save_view)
        self._delete_view=QPushButton('Delete'); self._delete_view.clicked.connect(self._delete_current_view); savedrow.addWidget(self._delete_view); root.addLayout(savedrow)
        searchrow=QHBoxLayout(); searchrow.addWidget(QLabel('Filter selectors:'))
        self._search=QLineEdit(); self._search.setPlaceholderText('Filter Album and Tag names…'); self._search.setClearButtonEnabled(True); self._search.textChanged.connect(self._render_lists); searchrow.addWidget(self._search,1); root.addLayout(searchrow)
        split=QSplitter(Qt.Orientation.Horizontal)
        aw=QWidget(); al=QVBoxLayout(aw); al.addWidget(QLabel('Albums')); self._album_list=QListWidget(); self._album_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); self._album_list.itemSelectionChanged.connect(self._selection_changed); al.addWidget(self._album_list,1); split.addWidget(aw)
        tw=QWidget(); tl=QVBoxLayout(tw); tl.addWidget(QLabel('Tags')); self._tag_list=QListWidget(); self._tag_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); self._tag_list.itemSelectionChanged.connect(self._selection_changed); tl.addWidget(self._tag_list,1); split.addWidget(tw)
        root.addWidget(split,1)
        self._recipe=QLabel('Select one or more Albums/Tags.'); self._recipe.setWordWrap(True); self._recipe.setObjectName('FieldKey'); root.addWidget(self._recipe)
        bottom=QHBoxLayout(); clear=QPushButton('Clear selection'); clear.clicked.connect(self._clear_selection); bottom.addWidget(clear); bottom.addStretch(1)
        self._browse=QPushButton('Browse intersection'); self._browse.clicked.connect(self._run); bottom.addWidget(self._browse); close=QPushButton('Close'); close.clicked.connect(self.close); bottom.addWidget(close); root.addLayout(bottom)
        self._reload_saved_views()
        self._render_lists()

    def _reload_saved_views(self, select_id=None):
        from ppa.db import connect
        from ppa.organization_views import list_organization_views
        conn=connect(self._db_path)
        try:
            views=list_organization_views(conn,library_id=self._library_id)
        finally:
            conn.close()
        self._saved.blockSignals(True)
        self._saved.clear(); self._saved.addItem("Current / unsaved",None)
        for v in views:
            self._saved.addItem(v.name,v.id)
        if select_id:
            idx=self._saved.findData(select_id); self._saved.setCurrentIndex(idx if idx >= 0 else 0)
        self._saved.blockSignals(False)
        self._delete_view.setEnabled(self._saved.currentData() is not None)

    def _apply_saved_view(self, *_):
        self._delete_view.setEnabled(self._saved.currentData() is not None)
        vid=self._saved.currentData()
        if not vid:
            return
        from ppa.db import connect
        from ppa.organization_views import get_organization_view
        conn=connect(self._db_path)
        try:
            view=get_organization_view(conn,str(vid))
        except Exception as exc:
            QMessageBox.warning(self,'Saved view',str(exc)); return
        finally:
            conn.close()
        if view.library_id != self._library_id:
            QMessageBox.warning(self,'Saved view','Saved organisation view belongs to a different Library.'); return
        if self._search.text():
            self._search.clear()
        self._album_list.clearSelection(); self._tag_list.clearSelection()
        wanted_a=set(view.album_ids); wanted_t=set(view.tag_ids)
        for i in range(self._album_list.count()):
            item=self._album_list.item(i); item.setSelected(str(item.data(_ID_ROLE)) in wanted_a)
        for i in range(self._tag_list.count()):
            item=self._tag_list.item(i); item.setSelected(str(item.data(_ID_ROLE)) in wanted_t)
        self._selection_changed()

    def _save_current_view(self):
        aids=self._selected(self._album_list); tids=self._selected(self._tag_list)
        if not aids and not tids:
            QMessageBox.information(self,'Saved view','Select at least one Album or Tag first.'); return
        default=self._saved.currentText() if self._saved.currentData() else ''
        name,ok=QInputDialog.getText(self,'Save organisation view','View name:',text=default)
        if not ok:
            return
        from ppa.db import connect
        from ppa.organization_views import save_organization_view
        conn=connect(self._db_path)
        try:
            view=save_organization_view(conn,library_id=self._library_id,name=name,album_ids=aids,tag_ids=tids)
        except Exception as exc:
            QMessageBox.warning(self,'Saved view',str(exc)); return
        finally:
            conn.close()
        self._reload_saved_views(view.id)

    def _delete_current_view(self):
        vid=self._saved.currentData()
        if not vid:
            return
        from ppa.db import connect
        from ppa.organization_views import delete_organization_view
        conn=connect(self._db_path)
        try:
            deleted=delete_organization_view(conn,str(vid))
        finally:
            conn.close()
        if deleted:
            self._reload_saved_views()

    def _render_lists(self, *_):
        q=self._search.text().casefold().strip()
        selected_a=set(self._selected(self._album_list)); selected_t=set(self._selected(self._tag_list))
        self._album_list.clear(); self._tag_list.clear()
        for c in self._albums.cards:
            if q and q not in c.search_text: continue
            i=QListWidgetItem(f'{c.name} ({c.photo_count})'); i.setData(_ID_ROLE,c.album_id); self._album_list.addItem(i); i.setSelected(c.album_id in selected_a)
        for c in self._tags.cards:
            if q and q not in c.search_text: continue
            i=QListWidgetItem(f'{c.name} ({c.photo_count})'); i.setData(_ID_ROLE,c.tag_id); self._tag_list.addItem(i); i.setSelected(c.tag_id in selected_t)
        self._selection_changed()

    def _selected(self, widget): return tuple(str(i.data(_ID_ROLE)) for i in widget.selectedItems() if i.data(_ID_ROLE))
    def _selection_changed(self):
        aids=self._selected(self._album_list); tids=self._selected(self._tag_list); self._browse.setEnabled(bool(aids or tids))
        amap={c.album_id:c.name for c in self._albums.cards}; tmap={c.tag_id:c.name for c in self._tags.cards}
        parts=[*(f'Album: {amap[a]}' for a in aids),*(f'Tag: {tmap[t]}' for t in tids)]
        self._recipe.setText(' ∩ '.join(parts) if parts else 'Select one or more Albums/Tags.')
    def _clear_selection(self): self._album_list.clearSelection(); self._tag_list.clearSelection(); self._selection_changed()
    def _run(self):
        aids=self._selected(self._album_list); tids=self._selected(self._tag_list)
        if not aids and not tids: return
        self._browse.setEnabled(False)
        self._progress=QProgressDialog('Building explicit organisation intersection…',None,0,0,self); self._progress.setWindowTitle('Organisational Discovery'); self._progress.setWindowModality(Qt.WindowModality.WindowModal); self._progress.show()
        w=OrganizationDiscoveryRunWorker(self._db_path,self._library_id,aids,tids); w.finished.connect(self._ready,Qt.ConnectionType.QueuedConnection); w.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection); self._registry.start(w)
    @Slot(object)
    def _ready(self,result):
        if self._progress: self._progress.close(); self._progress.deleteLater(); self._progress=None
        self._browse.setEnabled(True)
        from ppa.db import connect
        from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog
        # Browser uses the parent window's read-only result projection; its own connection is only for Preview.
        conn=connect(self._db_path)
        d=OrganizationBrowseDialog(conn,'organization_discovery',result.view.object_id,self,cache_dir=self._cache_dir/'results',view=result.view)
        d.finished.connect(conn.close); d.show(); self._browse_dialog=d
    @Slot(str)
    def _failed(self,message):
        if self._progress: self._progress.close(); self._progress.deleteLater(); self._progress=None
        self._browse.setEnabled(True); QMessageBox.warning(self,'Organisational Discovery',message)
    def closeEvent(self,event): self._registry.shutdown(); super().closeEvent(event)
