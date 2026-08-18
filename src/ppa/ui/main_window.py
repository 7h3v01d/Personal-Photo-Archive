"""Main application window.

Three panes — navigation, thumbnail grid, inspector — over a live status
bar. The window never issues SQL directly: it reads through
ppa.catalogue and drives work through the background workers. Scans and
verifies run off-thread so a 10,000-photo library never freezes the UI.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ppa import catalogue
from ppa.config import Config
from ppa.db import connect
from ppa.logging_setup import get_logger
from ppa.ui import theme
from ppa.ui.models import FILE_ID_ROLE, PhotoGridModel
from ppa.ui.workers import (
    ScanWorker,
    ThumbnailWorker,
    VerifyWorker,
    WorkerRegistry,
)

log = get_logger("ui")

_NAV = [
    ("All Photos", catalogue.VIEW_ALL),
    ("Recently Added", catalogue.VIEW_RECENT),
    ("Duplicates", catalogue.VIEW_DUPLICATES),
    ("Missing", catalogue.VIEW_MISSING),
]


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class InspectorPanel(QScrollArea):
    """Right-hand panel showing everything the catalogue knows about one file."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(14, 14, 14, 14)
        self._layout.setSpacing(6)
        self.setWidget(self._body)
        self.show_empty()

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _header(self, text: str) -> None:
        lbl = QLabel(text)
        lbl.setObjectName("SectionHeader")
        self._layout.addWidget(lbl)

    def _field(
        self, key: str, value: str, colour: str | None = None, max_len: int = 140
    ) -> None:
        row = QWidget()
        h = QVBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(1)
        k = QLabel(key.upper())
        k.setObjectName("FieldKey")

        shown = value if len(value) <= max_len else value[: max_len - 1] + "…"
        v = QLabel(shown)
        v.setObjectName("FieldVal")
        v.setWordWrap(True)
        v.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if len(value) > max_len:
            v.setToolTip(value)  # full text on hover
        if colour:
            v.setStyleSheet(f"color: {colour};")
        h.addWidget(k)
        h.addWidget(v)
        self._layout.addWidget(row)

    def show_empty(self) -> None:
        self._clear()
        title = QLabel("No selection")
        title.setObjectName("InspectorTitle")
        self._layout.addWidget(title)
        hint = QLabel("Select a photo to inspect its identity, metadata, and history.")
        hint.setObjectName("FieldKey")
        hint.setWordWrap(True)
        self._layout.addWidget(hint)
        self._layout.addStretch(1)

    def show_detail(self, d: catalogue.FileDetail, thumb: QPixmap | None) -> None:
        self._clear()

        title = QLabel(d.filename)
        title.setObjectName("InspectorTitle")
        title.setWordWrap(True)
        self._layout.addWidget(title)

        if thumb is not None and not thumb.isNull():
            pic = QLabel()
            pic.setPixmap(thumb.scaled(220, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._layout.addWidget(pic)

        self._field("status", d.status, theme.status_colour(d.status))

        self._header("FILE")
        self._field("path", d.path)
        if d.width_px and d.height_px:
            self._field("dimensions", f"{d.width_px} x {d.height_px}")
        self._field("size", _human_bytes(d.size_bytes))
        if d.mime_type:
            self._field("type", d.mime_type)
        if d.copy_count > 1:
            self._field("copies", f"{d.copy_count} files share this photo", theme.AMBER)

        self._header("IDENTITY")
        self._field("sha-256", d.sha256 or "not yet hashed",
                    None if d.sha256 else theme.TEXT_DIM)
        if d.camera:
            self._field("camera", d.camera)
        self._field("first seen", d.first_seen_at)
        self._field("last seen", d.last_seen_at)

        if d.integrity_events:
            self._header(f"INTEGRITY EVENTS ({len(d.integrity_events)})")
            for e in d.integrity_events[:12]:
                colour = theme.AMBER
                if e.event_type in ("hash_mismatch", "corrupt", "missing"):
                    colour = theme.RED
                elif e.event_type in ("move_confirmed", "restored"):
                    colour = theme.TEAL
                self._field(e.event_type, e.detail or "", colour)

        if len(d.path_history) > 1:
            self._header("PATH HISTORY")
            for h in d.path_history:
                self._field(h.observed_at, h.path)

        self._layout.addStretch(1)


class MainWindow(QMainWindow):
    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self._conn = connect(config.db_path)
        self._cache_dir = config.db_path.parent / "thumbnails"
        self._registry = WorkerRegistry()
        self._current_view = catalogue.VIEW_ALL
        self._current_library: Path | None = None
        self._busy = False

        self.setWindowTitle("Personal Photo Archive")
        self.resize(1200, 780)

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._start_thumbnail_worker()

        self._resolve_initial_library()
        self.refresh()

    # --- construction -------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        self._act_add = QAction("Add Library…", self)
        self._act_add.triggered.connect(self._on_add_library)
        tb.addAction(self._act_add)

        self._act_scan = QAction("Scan", self)
        self._act_scan.triggered.connect(self._on_scan)
        tb.addAction(self._act_scan)

        self._act_verify = QAction("Verify", self)
        self._act_verify.triggered.connect(self._on_verify)
        tb.addAction(self._act_verify)

        self._act_refresh = QAction("Refresh", self)
        self._act_refresh.triggered.connect(self.refresh)
        tb.addAction(self._act_refresh)

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        # Nav
        self._nav = QListWidget()
        self._nav.setFixedWidth(200)
        for label, _view in _NAV:
            self._nav.addItem(QListWidgetItem(label))
        self._nav.setCurrentRow(0)
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        splitter.addWidget(self._nav)

        # Grid
        self._model = PhotoGridModel()
        self._model.request_thumbnail.connect(self._on_thumbnail_requested)
        self._grid = QListView()
        self._grid.setModel(self._model)
        self._grid.setViewMode(QListView.IconMode)
        self._grid.setResizeMode(QListView.Adjust)
        self._grid.setMovement(QListView.Static)
        self._grid.setIconSize(QSize(180, 180))
        self._grid.setGridSize(QSize(210, 230))
        self._grid.setSpacing(8)
        self._grid.setUniformItemSizes(True)
        self._grid.setSelectionMode(QListView.SingleSelection)
        self._grid.selectionModel().currentChanged.connect(self._on_selection)
        splitter.addWidget(self._grid)

        # Inspector
        self._inspector = InspectorPanel()
        self._inspector.setMinimumWidth(300)
        splitter.addWidget(self._inspector)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([200, 660, 340])
        self.setCentralWidget(splitter)

    def _build_statusbar(self) -> None:
        self._status = self.statusBar()
        self._status.showMessage("Ready.")

    def _start_thumbnail_worker(self) -> None:
        self._thumb_worker = ThumbnailWorker(self._cache_dir, size=256)
        self._thumb_worker.ready.connect(self._on_thumbnail_ready)
        self._registry.start_persistent(self._thumb_worker)

    # --- data / refresh -----------------------------------------------------
    def _resolve_initial_library(self) -> None:
        if self._config.library_directories:
            self._current_library = self._config.library_directories[0]
            return
        stats = catalogue.library_stats(self._conn)
        if stats.last_library_path:
            self._current_library = Path(stats.last_library_path)

    def refresh(self) -> None:
        items = catalogue.grid_items(self._conn, self._current_view)
        self._model.set_items(items)
        self._inspector.show_empty()
        self._update_status_summary()

    def _update_status_summary(self) -> None:
        s = catalogue.library_stats(self._conn)
        parts = [
            f"{s.photos} photos",
            f"{s.files} files",
            _human_bytes(s.total_bytes),
        ]
        if s.duplicate_files:
            parts.append(f"{s.duplicate_files} dup-files")
        if s.missing:
            parts.append(f"{s.missing} missing")
        if s.hash_mismatches:
            parts.append(f"{s.hash_mismatches} hash mismatches")
        lib = str(self._current_library) if self._current_library else "no library set"
        self._status.showMessage("   |   ".join(parts) + f"   |   {lib}")

    # --- nav / selection ----------------------------------------------------
    def _on_nav_changed(self, row: int) -> None:
        if 0 <= row < len(_NAV):
            self._current_view = _NAV[row][1]
            self.refresh()

    def _on_selection(self, current: QModelIndex, _previous: QModelIndex) -> None:
        item = self._model.item_at(current)
        if item is None:
            self._inspector.show_empty()
            return
        detail = catalogue.file_detail(self._conn, item.file_id)
        if detail is None:
            self._inspector.show_empty()
            return
        thumb = self._model._pixmaps.get(item.file_id)
        self._inspector.show_detail(detail, thumb)

    # --- thumbnails ---------------------------------------------------------
    def _on_thumbnail_requested(self, file_id: str, path: str, sha256: object) -> None:
        # Forward the model's request to the persistent worker thread.
        self._thumb_worker.request(file_id, path, sha256)

    def _on_thumbnail_ready(self, file_id: str, image) -> None:
        self._model.set_thumbnail(file_id, QPixmap.fromImage(image))

    # --- scan / verify ------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for act in (self._act_add, self._act_scan, self._act_verify, self._act_refresh):
            act.setEnabled(not busy)

    def _on_add_library(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a photo library")
        if not directory:
            return
        self._current_library = Path(directory)
        self._update_status_summary()
        if self._confirm(f"Scan {directory} now?"):
            self._on_scan()

    def _on_scan(self) -> None:
        if self._busy:
            return
        if self._current_library is None:
            self._on_add_library()
            return
        if not self._current_library.is_dir():
            self._warn(f"Not a directory: {self._current_library}")
            return

        self._set_busy(True)
        self._status.showMessage("Starting scan…")
        worker = ScanWorker(self._config.db_path, self._current_library)
        worker.progress.connect(self._status.showMessage)
        worker.finished.connect(self._on_scan_done)
        worker.failed.connect(self._on_worker_failed)
        self._registry.start(worker)

    def _on_scan_done(self, report) -> None:
        self._set_busy(False)
        self.refresh()
        self._status.showMessage(
            f"Scan complete — {report.new_files} new, {report.moved_files} moved, "
            f"{report.duplicate_files} duplicates, {report.missing_files} missing."
        )

    def _on_verify(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Starting integrity verification…")
        worker = VerifyWorker(self._config.db_path)
        worker.progress.connect(self._status.showMessage)
        worker.finished.connect(self._on_verify_done)
        worker.failed.connect(self._on_worker_failed)
        self._registry.start(worker)

    def _on_verify_done(self, report) -> None:
        self._set_busy(False)
        self.refresh()
        msg = (
            f"{report.verified_ok} ok, {report.mismatches} mismatches, "
            f"{report.now_missing} missing, {report.corrupt} unreadable."
        )
        self._status.showMessage("Verification complete — " + msg)
        if report.mismatches or report.corrupt or report.now_missing:
            QMessageBox.warning(self, "Integrity issues found", msg)

    def _on_worker_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Operation failed: {message}")
        self._status.showMessage("Operation failed.")

    # --- small dialog helpers ----------------------------------------------
    def _confirm(self, text: str) -> bool:
        return (
            QMessageBox.question(self, "Personal Photo Archive", text)
            == QMessageBox.StandardButton.Yes
        )

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, "Personal Photo Archive", text)

    def closeEvent(self, event) -> None:
        self._registry.shutdown()
        super().closeEvent(event)
