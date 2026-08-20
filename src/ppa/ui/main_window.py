"""Main application window.

Three panes — navigation, thumbnail grid, inspector — over a live status
bar. The window never issues SQL directly: it reads through
ppa.catalogue and drives work through the background workers. Scans and
verifies run off-thread so a 10,000-photo library never freezes the UI.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ppa import catalogue
from ppa.config import Config
from ppa.db import connect
from ppa.logging_setup import get_logger
from ppa.ui import theme
from ppa.ui.delegate import PhotoTileDelegate
from ppa.ui.gpsmap import GpsMiniMap
from ppa.ui.models import FILE_ID_ROLE, PhotoGridModel
from ppa.ui.workers import (
    MetadataWorker,
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
        self._path: str | None = None
        self.show_empty()

    def _open_folder(self) -> None:
        if not self._path:
            return
        folder = Path(self._path).parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _copy_path(self) -> None:
        if self._path:
            QApplication.clipboard().setText(self._path)

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
        self._path = None
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
        self._path = d.path

        title = QLabel(d.filename)
        title.setObjectName("InspectorTitle")
        title.setWordWrap(True)
        self._layout.addWidget(title)

        # Quick actions: open the containing folder / copy the full path.
        actions = QWidget()
        arow = QHBoxLayout(actions)
        arow.setContentsMargins(0, 0, 0, 0)
        arow.setSpacing(6)
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open_folder)
        copy_btn = QPushButton("Copy path")
        copy_btn.clicked.connect(self._copy_path)
        arow.addWidget(open_btn)
        arow.addWidget(copy_btn)
        arow.addStretch(1)
        self._layout.addWidget(actions)

        if thumb is not None and not thumb.isNull():
            pic = QLabel()
            pic.setPixmap(thumb.scaled(220, 220, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))
            self._layout.addWidget(pic)

        self._field("status", d.status, theme.status_colour(d.status))
        if d.health_status and d.health_status != "ok":
            self._field("health", d.health_status, theme.RED)

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

        if d.observed_metadata:
            self._header("METADATA (OBSERVED)")
            for label, value in d.observed_metadata:
                if label == "GPS":
                    continue  # shown on the mini-map instead of as text
                self._field(label, value)

        if d.gps is not None:
            self._header("LOCATION (OBSERVED)")
            mini = GpsMiniMap()
            mini.set_coords(d.gps[0], d.gps[1])
            self._layout.addWidget(mini)

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

        self._act_extract = QAction("Extract Metadata", self)
        self._act_extract.triggered.connect(lambda: self._start_metadata(auto=False))
        tb.addAction(self._act_extract)

        self._act_refresh = QAction("Refresh", self)
        self._act_refresh.triggered.connect(self.refresh)
        tb.addAction(self._act_refresh)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(QLabel("Size "))
        self._density = QComboBox()
        self._density.addItems(["Small", "Medium", "Large"])
        self._density.setCurrentIndex(1)
        self._density.currentIndexChanged.connect(self._on_density_changed)
        tb.addWidget(self._density)

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
        self._grid = QListView()
        self._grid.setModel(self._model)
        self._grid.setViewMode(QListView.ViewMode.IconMode)
        self._grid.setResizeMode(QListView.ResizeMode.Adjust)
        self._grid.setMovement(QListView.Movement.Static)
        self._grid.setItemDelegate(PhotoTileDelegate())
        self._grid.setSpacing(8)
        self._grid.setUniformItemSizes(True)
        self._grid.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._grid.selectionModel().currentChanged.connect(self._on_selection)
        self._apply_density(1)  # Medium

        # Empty-state page, shown when the current view has no photos.
        self._empty = QLabel()
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setObjectName("FieldKey")
        self._empty.setWordWrap(True)

        self._center = QStackedWidget()
        self._center.addWidget(self._grid)   # index 0
        self._center.addWidget(self._empty)  # index 1
        splitter.addWidget(self._center)

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
        # Genuine cross-thread dispatch: the model's request signal is
        # delivered to the worker's slot on the worker thread (queued), so the
        # Pillow decode never runs on the GUI thread.
        self._model.request_thumbnail.connect(
            self._thumb_worker.request, Qt.ConnectionType.QueuedConnection
        )

    # --- data / refresh -----------------------------------------------------
    _DENSITY = {0: (120, 150), 1: (180, 210), 2: (250, 280)}  # icon, cell

    def _apply_density(self, index: int) -> None:
        icon, cell = self._DENSITY.get(index, self._DENSITY[1])
        self._grid.setIconSize(QSize(icon, icon))
        self._grid.setGridSize(QSize(cell, cell + 20))

    def _on_density_changed(self, index: int) -> None:
        self._apply_density(index)
        self._grid.doItemsLayout()

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
        if items:
            self._center.setCurrentIndex(0)
        else:
            self._empty.setText(self._empty_message())
            self._center.setCurrentIndex(1)
        self._update_status_summary()

    def _empty_message(self) -> str:
        if self._current_library is None:
            return "No library yet.\n\nUse “Add Library…” to point the archive at a\nfolder of photos, then Scan."
        messages = {
            catalogue.VIEW_ALL: "No photos catalogued yet.\n\nPress Scan to index the current library.",
            catalogue.VIEW_RECENT: "Nothing recently added.",
            catalogue.VIEW_DUPLICATES: "No duplicates found. Every photo is unique.",
            catalogue.VIEW_MISSING: "No missing files. Every catalogued photo is present.",
        }
        return messages.get(self._current_view, "Nothing to show.")

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
    def _on_thumbnail_ready(self, file_id: str, image) -> None:
        self._model.set_thumbnail(file_id, QPixmap.fromImage(image))

    # --- scan / verify ------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for act in (self._act_add, self._act_scan, self._act_verify,
                    self._act_extract, self._act_refresh):
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
        data_dir = self._config.db_path.parent
        protected = [self._config.db_path, data_dir / "thumbnails", self._config.log_path]
        worker = ScanWorker(
            self._config.db_path, self._current_library, protected_paths=protected
        )
        worker.progress.connect(self._status.showMessage)
        worker.finished.connect(self._on_scan_done)
        worker.failed.connect(self._on_worker_failed)
        self._registry.start(worker)

    def _on_scan_done(self, report) -> None:
        self.refresh()
        self._status.showMessage(
            f"Scan complete — {report.new_files} new, {report.moved_files} moved, "
            f"{report.duplicate_files} duplicates, {report.missing_files} missing. "
            "Reading metadata…"
        )
        # Chain straight into metadata extraction so camera/date fields fill in.
        self._start_metadata(auto=True)

    def _start_metadata(self, *, auto: bool) -> None:
        if self._busy and not auto:
            return
        self._set_busy(True)
        if not auto:
            self._status.showMessage("Reading metadata…")
        worker = MetadataWorker(self._config.db_path)
        worker.progress.connect(self._status.showMessage)
        worker.finished.connect(self._on_metadata_done)
        worker.failed.connect(self._on_worker_failed)
        self._registry.start(worker)

    def _on_metadata_done(self, count: int) -> None:
        self._set_busy(False)
        # Metadata doesn't change which files are in the grid, so don't rebuild
        # it (that would drop the selection). Just refresh the status summary
        # and re-show the selected file so its new metadata appears.
        self._update_status_summary()
        current = self._grid.currentIndex()
        if current.isValid():
            self._on_selection(current, current)
        self._status.showMessage(
            f"Metadata read for {count} file(s)." if count
            else "Metadata up to date."
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
