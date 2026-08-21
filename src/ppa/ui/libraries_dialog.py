"""Library (resource) management dialog.

Shows every folder the archive is drawing photos from, with live present/missing
counts and availability, and lets the person add, rescan, set-as-target, and
remove libraries. Removal only forgets catalogue records — it never touches a
source photograph.

Scans and removals are not run here: the dialog records a *request* (a chosen
path to scan, a library id to forget, a path to make the scan target) and the
main window carries it out through its off-thread worker machinery. This keeps
all heavy/off-thread work in one place and the dialog purely presentational.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ppa import catalogue
from ppa.ui import theme


def _fmt_scan(when: str | None) -> str:
    if not when:
        return "never"
    # Stored ISO timestamp; show just the date-time to the minute.
    return when.replace("T", " ")[:16]


class LibrariesDialog(QDialog):
    """Modal resource manager. After exec(), the caller reads:

    * ``scan_request`` — a Path the person asked to scan/rescan (or None)
    * ``target_request`` — a Path to become the current scan target (or None)
    """

    def __init__(self, conn, current_target: Path | None, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._current_target = current_target
        self.scan_request: Path | None = None
        self.target_request: Path | None = None

        self.setWindowTitle("Manage Libraries")
        self.setModal(True)
        self.resize(760, 420)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Folders the archive draws photos from. Removing a library only makes "
            "the archive forget it — your photo files are never changed or deleted."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(intro)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Folder", "Status", "Photos", "Last scan"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._sync_buttons)
        root.addWidget(self._table, 1)

        # Buttons
        btns = QHBoxLayout()
        self._add = QPushButton("Add Library…")
        self._add.clicked.connect(self._on_add)
        self._rescan = QPushButton("Rescan")
        self._rescan.clicked.connect(self._on_rescan)
        self._target = QPushButton("Set as Scan Target")
        self._target.clicked.connect(self._on_set_target)
        self._remove = QPushButton("Remove…")
        self._remove.clicked.connect(self._on_remove)
        for b in (self._add, self._rescan, self._target, self._remove):
            btns.addWidget(b)
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        root.addLayout(btns)

        self._reload()

    # --- data ---------------------------------------------------------------
    def _reload(self) -> None:
        libs = catalogue.list_libraries(self._conn)
        self._libs = libs
        self._table.setRowCount(len(libs))
        for row, lib in enumerate(libs):
            path_item = QTableWidgetItem(lib.display_path)
            path_item.setData(Qt.ItemDataRole.UserRole, lib.id)
            if self._current_target is not None \
                    and str(self._current_target) == lib.display_path:
                path_item.setText(f"● {lib.display_path}")   # marks the scan target

            if not lib.available:
                status, colour = "offline", theme.AMBER
            elif lib.state == "unavailable":
                status, colour = "unavailable", theme.AMBER
            elif lib.missing:
                status, colour = f"{lib.missing} missing", theme.RED
            else:
                status, colour = "active", theme.PHOSPHOR
            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor(colour))

            photos_item = QTableWidgetItem(str(lib.present))
            photos_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            scan_item = QTableWidgetItem(_fmt_scan(lib.last_scan_at))

            self._table.setItem(row, 0, path_item)
            self._table.setItem(row, 1, status_item)
            self._table.setItem(row, 2, photos_item)
            self._table.setItem(row, 3, scan_item)
        self._sync_buttons()

    def _selected(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        return self._libs[rows[0].row()]

    def _sync_buttons(self) -> None:
        has = self._selected() is not None
        for b in (self._rescan, self._target, self._remove):
            b.setEnabled(has)

    # --- actions ------------------------------------------------------------
    def _on_add(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a photo folder to add")
        if not directory:
            return
        self.scan_request = Path(directory)
        self.target_request = Path(directory)
        self.accept()   # hand back to the main window to scan off-thread

    def _on_rescan(self) -> None:
        lib = self._selected()
        if lib is None:
            return
        if not lib.available:
            QMessageBox.warning(self, "Manage Libraries",
                                f"That folder isn't reachable right now:\n{lib.display_path}")
            return
        self.scan_request = Path(lib.display_path)
        self.target_request = Path(lib.display_path)
        self.accept()

    def _on_set_target(self) -> None:
        lib = self._selected()
        if lib is None:
            return
        self.target_request = Path(lib.display_path)
        self.accept()

    def _on_remove(self) -> None:
        lib = self._selected()
        if lib is None:
            return
        count = lib.present + lib.missing
        confirmed = QMessageBox.question(
            self, "Remove library",
            f"Remove this library from the archive?\n\n{lib.display_path}\n\n"
            f"This forgets {count} catalogued photo record(s). Your photo files on "
            "disk are NOT changed or deleted — only the archive's record of them.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        try:
            catalogue.forget_library(self._conn, lib.id)
        except Exception as exc:   # pragma: no cover - surfaced to the user
            QMessageBox.critical(self, "Remove library", f"Could not remove: {exc}")
            return
        if self._current_target is not None and str(self._current_target) == lib.display_path:
            self._current_target = None
            self.target_request = None
        self._reload()
