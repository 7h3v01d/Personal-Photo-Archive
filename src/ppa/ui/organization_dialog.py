"""Phase 9.1 — desktop Album/Tag curation over logical Photo identity."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QTabWidget, QVBoxLayout, QWidget, QInputDialog,
)

from ppa.organization import (
    bulk_add_photos_to_album, bulk_remove_photos_from_album, bulk_tag_photos,
    bulk_untag_photos, create_album, create_tag, list_albums, list_tags,
)

_ID_ROLE = Qt.ItemDataRole.UserRole


class OrganizationDialog(QDialog):
    changed = Signal()

    def __init__(self, conn, library_id: int, photo_ids: tuple[str, ...], parent=None):
        super().__init__(parent)
        self._conn = conn
        self._library_id = library_id
        self._photo_ids = tuple(dict.fromkeys(photo_ids))
        self.setWindowTitle("Albums & Tags")
        self.resize(640, 500)
        root = QVBoxLayout(self)
        self._summary = QLabel()
        root.addWidget(self._summary)
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)
        self._album_list = QListWidget(); self._tag_list = QListWidget()
        self._album_list.itemDoubleClicked.connect(lambda *_: self._browse_album())
        self._tag_list.itemDoubleClicked.connect(lambda *_: self._browse_tag())
        self._tabs.addTab(self._make_tab(self._album_list, "New album…", self._new_album,
                                         "Add selected", self._album_add,
                                         "Remove selected", self._album_remove,
                                         "Browse…", self._browse_album), "Albums")
        self._tabs.addTab(self._make_tab(self._tag_list, "New tag…", self._new_tag,
                                         "Apply tag", self._tag_add,
                                         "Remove tag", self._tag_remove,
                                         "Browse…", self._browse_tag), "Tags")
        row = QHBoxLayout()
        present = QPushButton("Album presentation…"); present.clicked.connect(self._album_presentation); row.addWidget(present)
        row.addStretch(1)
        close = QPushButton("Close"); close.clicked.connect(self.accept); row.addWidget(close); root.addLayout(row)
        self.refresh()

    def _make_tab(self, view, create_text, create_cb, add_text, add_cb, remove_text, remove_cb, browse_text, browse_cb):
        w = QWidget(); v = QVBoxLayout(w); v.addWidget(view, 1)
        row = QHBoxLayout();
        for text, cb in ((create_text, create_cb), (add_text, add_cb), (remove_text, remove_cb), (browse_text, browse_cb)):
            b = QPushButton(text); b.clicked.connect(cb); row.addWidget(b)
        row.addStretch(1); v.addLayout(row); return w

    def refresh(self):
        self._summary.setText(f"{len(self._photo_ids)} selected logical photo{'s' if len(self._photo_ids) != 1 else ''}")
        self._album_list.clear()
        for a in list_albums(self._conn, library_id=self._library_id):
            i = QListWidgetItem(f"{a.name}  ·  {len(a.photo_ids)} photos"); i.setData(_ID_ROLE, a.id); self._album_list.addItem(i)
        self._tag_list.clear()
        for t in list_tags(self._conn, library_id=self._library_id):
            i = QListWidgetItem(f"{t.name}  ·  {len(t.photo_ids)} photos"); i.setData(_ID_ROLE, t.id); self._tag_list.addItem(i)

    def _chosen_id(self, view):
        item = view.currentItem()
        if item is None:
            QMessageBox.information(self, "Albums & Tags", "Select an item first.")
            return None
        return item.data(_ID_ROLE)

    def _run(self, fn, object_id):
        if not self._photo_ids:
            return
        try:
            fn(self._conn, object_id, self._photo_ids)
        except Exception as exc:
            QMessageBox.critical(self, "Albums & Tags", str(exc)); return
        self.refresh(); self.changed.emit()

    def _new_album(self):
        name, ok = QInputDialog.getText(self, "New album", "Album name:")
        if ok and name.strip():
            try: create_album(self._conn, library_id=self._library_id, name=name)
            except Exception as exc: QMessageBox.critical(self, "New album", str(exc)); return
            self.refresh(); self.changed.emit()

    def _new_tag(self):
        name, ok = QInputDialog.getText(self, "New tag", "Tag name:")
        if ok and name.strip():
            try: create_tag(self._conn, library_id=self._library_id, name=name)
            except Exception as exc: QMessageBox.critical(self, "New tag", str(exc)); return
            self.refresh(); self.changed.emit()


    def _browse(self, kind, view):
        oid = self._chosen_id(view)
        if not oid:
            return
        from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog
        dialog = OrganizationBrowseDialog(self._conn, kind, oid, self)
        dialog.show()

    def _browse_album(self):
        self._browse("album", self._album_list)

    def _browse_tag(self):
        self._browse("tag", self._tag_list)


    def _album_presentation(self):
        oid = self._chosen_id(self._album_list)
        if not oid:
            return
        from ppa.ui.album_presentation_dialog import AlbumPresentationDialog
        dialog = AlbumPresentationDialog(self._conn, oid, self)
        dialog.exec()
        self.refresh(); self.changed.emit()

    def _album_add(self):
        oid=self._chosen_id(self._album_list)
        if oid: self._run(bulk_add_photos_to_album, oid)
    def _album_remove(self):
        oid=self._chosen_id(self._album_list)
        if oid: self._run(bulk_remove_photos_from_album, oid)
    def _tag_add(self):
        oid=self._chosen_id(self._tag_list)
        if oid: self._run(bulk_tag_photos, oid)
    def _tag_remove(self):
        oid=self._chosen_id(self._tag_list)
        if oid: self._run(bulk_untag_photos, oid)
