"""Phase 9.3 — human Album presentation editor."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout

from ppa.organization import get_album, get_album_presentation, reset_album_presentation, set_album_cover, set_album_presentation_order
from ppa.organization_browse import build_organization_browse

PHOTO_ROLE = Qt.ItemDataRole.UserRole

class AlbumPresentationDialog(QDialog):
    def __init__(self, conn, album_id: str, parent=None):
        super().__init__(parent)
        self._conn=conn; self._album_id=album_id
        self.setWindowTitle('Album presentation')
        self.resize(650,560)
        root=QVBoxLayout(self)
        album=get_album(conn, album_id)
        root.addWidget(QLabel(f'<b>{album.name}</b>'))
        root.addWidget(QLabel('Presentation only — cover and reading order never change chronology or Album membership.'))
        self._list=QListWidget(); root.addWidget(self._list,1)
        row=QHBoxLayout()
        for text, cb in [('Set cover',self._cover),('Move up',lambda:self._move(-1)),('Move down',lambda:self._move(1)),('Save order',self._save),('Reset',self._reset)]:
            b=QPushButton(text); b.clicked.connect(cb); row.addWidget(b)
        root.addLayout(row)
        close=QPushButton('Close'); close.clicked.connect(self.accept); root.addWidget(close)
        self.refresh()

    def refresh(self):
        self._list.clear()
        view=build_organization_browse(self._conn, object_kind='album', object_id=self._album_id)
        pres=get_album_presentation(self._conn,self._album_id)
        for item in view.items:
            text=('★ ' if item.photo_id==pres.cover_photo_id else '')+item.filename
            row=QListWidgetItem(text); row.setData(PHOTO_ROLE,item.photo_id); self._list.addItem(row)

    def _selected(self):
        i=self._list.currentItem(); return i.data(PHOTO_ROLE) if i else None

    def _cover(self):
        pid=self._selected()
        if not pid: return
        try: set_album_cover(self._conn,self._album_id,pid)
        except Exception as exc: QMessageBox.critical(self,'Album presentation',str(exc)); return
        self.refresh()

    def _move(self, delta):
        row=self._list.currentRow(); new=row+delta
        if row < 0 or new < 0 or new >= self._list.count(): return
        item=self._list.takeItem(row); self._list.insertItem(new,item); self._list.setCurrentRow(new)

    def _save(self):
        ids=tuple(self._list.item(i).data(PHOTO_ROLE) for i in range(self._list.count()))
        try: set_album_presentation_order(self._conn,self._album_id,ids)
        except Exception as exc: QMessageBox.critical(self,'Album presentation',str(exc)); return
        self.refresh()

    def _reset(self):
        try: reset_album_presentation(self._conn,self._album_id)
        except Exception as exc: QMessageBox.critical(self,'Album presentation',str(exc)); return
        self.refresh()
