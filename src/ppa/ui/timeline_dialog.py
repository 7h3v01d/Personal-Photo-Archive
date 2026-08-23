"""Phase 8.0 read-only chronology timeline browser."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ppa import catalogue
from ppa.ui.models import PhotoGridModel


_LANES = (
    ("placed", "Placed"),
    ("range", "Ranges"),
    ("tentative", "Tentative"),
    ("unplaced", "Unplaced"),
)


class TimelineDialog(QDialog):
    def __init__(self, conn, view, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._view = view
        self.setWindowTitle("Chronology Timeline")
        self.resize(980, 680)

        root = QVBoxLayout(self)
        counts = view.lanes
        summary = QLabel(
            f"{len(view.items)} photos · {counts['placed'].count} placed · "
            f"{counts['range'].count} ranges · {counts['tentative'].count} tentative · "
            f"{counts['unplaced'].count} unplaced")
        summary.setWordWrap(True)
        root.addWidget(summary)

        note = QLabel(
            "Confirmed/current chronology is separated from tentative proposals and unresolved dates. "
            "Ranges are preserved; stale interpretations never place a photo.")
        note.setWordWrap(True)
        note.setObjectName("FieldKey")
        root.addWidget(note)

        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)
        self._trees = {}
        for lane, label in _LANES:
            tree = QTreeWidget()
            tree.setColumnCount(5)
            tree.setHeaderLabels(["Date", "Photo", "Source", "Confidence", "Why / state"])
            tree.setAlternatingRowColors(True)
            tree.setRootIsDecorated(True)
            tree.itemDoubleClicked.connect(self._open_item)
            self._tabs.addTab(tree, f"{label} ({counts[lane].count})")
            self._trees[lane] = tree

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        root.addLayout(buttons)

        self._populate()

    def _populate(self) -> None:
        for lane, _label in _LANES:
            tree = self._trees[lane]
            items = [i for i in self._view.items if i.lane == lane]
            if lane == "unplaced":
                parent = QTreeWidgetItem(["Unplaced", "", "", "", ""])
                tree.addTopLevelItem(parent)
                for item in items:
                    self._add_photo(parent, item)
                parent.setExpanded(True)
                continue

            groups = {}
            for item in items:
                year = (item.start_date or "Unknown")[:4]
                month = (item.start_date or "Unknown")[:7]
                y = groups.setdefault(year, {})
                y.setdefault(month, []).append(item)
            for year in sorted(groups):
                year_item = QTreeWidgetItem([year, "", "", "", ""])
                tree.addTopLevelItem(year_item)
                for month in sorted(groups[year]):
                    month_item = QTreeWidgetItem([month, "", "", "", ""])
                    year_item.addChild(month_item)
                    for item in groups[year][month]:
                        self._add_photo(month_item, item)
                year_item.setExpanded(True)
        for tree in self._trees.values():
            for col in range(5):
                tree.resizeColumnToContents(col)

    @staticmethod
    def _date_text(item) -> str:
        if item.start_date is None:
            return "—"
        if item.end_date:
            return f"{item.start_date} … {item.end_date}"
        return item.start_date

    def _add_photo(self, parent, item) -> None:
        row = QTreeWidgetItem([
            self._date_text(item), item.filename, item.source.replace("_", " "),
            item.confidence or item.reliability, item.reason,
        ])
        row.setData(0, Qt.ItemDataRole.UserRole, item.file_id)
        parent.addChild(row)

    def _open_item(self, row, _column) -> None:
        fid = row.data(0, Qt.ItemDataRole.UserRole)
        if not fid:
            return
        lane = next((i.lane for i in self._view.items if i.file_id == fid), None)
        ids = [i.file_id for i in self._view.items if i.lane == lane] if lane else [fid]
        model = PhotoGridModel()
        grid_items = catalogue.grid_items_for_files(self._conn, ids)
        model.set_items(grid_items)
        index = next((n for n, x in enumerate(grid_items) if x.file_id == fid), 0)
        from ppa.ui.preview_dialog import PreviewDialog
        dialog = PreviewDialog(self._conn, model, index, self,
                               window_title=f"Timeline — {lane.title() if lane else 'Photo'}")
        dialog._timeline_model = model
        dialog.show()
