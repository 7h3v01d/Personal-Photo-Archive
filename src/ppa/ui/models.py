"""Grid model.

A QAbstractListModel over a list of catalogue.GridItem. Thumbnails load
lazily: the first time a row is painted, the model asks (once) for its
thumbnail via the request_thumbnail signal, showing a placeholder tile
until the real image arrives through set_thumbnail().
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QPixmap

from ppa.catalogue import GridItem
from ppa.ui import theme

FILE_ID_ROLE = Qt.ItemDataRole.UserRole + 1
STATUS_ROLE = Qt.ItemDataRole.UserRole + 2
COPY_COUNT_ROLE = Qt.ItemDataRole.UserRole + 3

_THUMB = 256


def _placeholder(status: str) -> QPixmap:
    pm = QPixmap(_THUMB, _THUMB)
    pm.fill(QColor(theme.PANEL))
    return pm


class PhotoGridModel(QAbstractListModel):
    request_thumbnail = Signal(str, str, str)  # file_id, path, sha256 ("" if none)

    def __init__(self) -> None:
        super().__init__()
        self._items: list[GridItem] = []
        self._pixmaps: dict[str, QPixmap] = {}
        self._requested: set[str] = set()

    # --- population ---------------------------------------------------------
    def set_items(self, items: list[GridItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._pixmaps.clear()
        self._requested.clear()
        self.endResetModel()

    def item_at(self, index: QModelIndex) -> GridItem | None:
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        return self._items[index.row()]

    # --- thumbnails ---------------------------------------------------------
    def set_thumbnail(self, file_id: str, pixmap: QPixmap) -> None:
        self._pixmaps[file_id] = pixmap
        for row, item in enumerate(self._items):
            if item.file_id == file_id:
                idx = self.index(row)
                self.dataChanged.emit(idx, idx, [Qt.DecorationRole])
                break

    # --- QAbstractListModel -------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        item = self.item_at(index)
        if item is None:
            return None

        if role == Qt.DecorationRole:
            pm = self._pixmaps.get(item.file_id)
            if pm is not None:
                return pm
            if item.file_id not in self._requested and item.status != "missing":
                self._requested.add(item.file_id)
                self.request_thumbnail.emit(item.file_id, item.path, item.sha256 or "")
            return _placeholder(item.status)

        if role == Qt.DisplayRole:
            return item.filename
        if role == Qt.ToolTipRole:
            return f"{item.path}\n{item.status}"
        if role == FILE_ID_ROLE:
            return item.file_id
        if role == STATUS_ROLE:
            return item.status
        if role == COPY_COUNT_ROLE:
            return item.copy_count
        return None
