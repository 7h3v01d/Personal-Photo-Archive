"""Main application window.

Three panes — navigation, thumbnail grid, inspector — over a live status
bar. The window never issues SQL directly: it reads through
ppa.catalogue and drives work through the background workers. Scans and
verifies run off-thread so a 10,000-photo library never freezes the UI.
"""

from __future__ import annotations

from pathlib import Path
import time

from PySide6.QtCore import QModelIndex, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QPixmap, QShortcut
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
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from ppa import catalogue
from ppa import organization
from ppa.config import Config
from ppa.db import connect
from ppa.logging_setup import get_logger
from ppa.activity_runs import new_run_id, run_extra
from ppa.ui import theme
from ppa.ui.delegate import PhotoTileDelegate
from ppa.ui.command_palette_dialog import CommandPaletteDialog, PaletteCommand
from ppa.ui.gpsmap import GpsMiniMap
from ppa.ui.models import FILE_ID_ROLE, PhotoGridModel
from ppa.ui.workers import (
    DateReviewQueueWorker,
    UnresolvedMemoriesWorker,
    PilotAuditWorker,
    MetadataWorker,
    ScanWorker,
    ThumbnailWorker,
    VerifyWorker,
    TimelineWorker,
    EventHomeWorker,
    AlbumHomeWorker,
    TagHomeWorker,
    OrganizationDiscoveryHomeWorker,
    OrganizationHealthWorker,
    ArchiveHealthWorker,
    MismatchInvestigationWorker,
    MismatchResolutionWorker,
    RecoveryPlanningWorker,
    RecoveryProposalWorker,
    RecoveryPreservationWorker,
    RecoveryDonorMaterializationWorker,
    OrganizationSuggestionsWorker,
    OrganizationActivityWorker,
    OrganizationReportWorker,
    DuplicateLineageReviewWorker,
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

    investigate_mismatch_requested = Signal(str)

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

    def show_detail(self, d: catalogue.FileDetail, thumb: QPixmap | None, *, albums=(), tags=()) -> None:
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

        if albums or tags:
            self._header("ORGANISATION")
            if albums:
                self._field("albums", ", ".join(a.name for a in albums))
            if tags:
                self._field("tags", ", ".join(t.name for t in tags))

        self._field("status", d.status, theme.status_colour(d.status))
        if d.health_status and d.health_status != "ok":
            self._field("health", d.health_status, theme.RED)
        if d.health_status == "hash_mismatch":
            investigate = QPushButton("Investigate hash mismatch…")
            investigate.setToolTip(
                "Compare the expected FileRevision image with the current untrusted on-disk bytes."
            )
            investigate.clicked.connect(
                lambda _checked=False, fid=d.file_id: self.investigate_mismatch_requested.emit(fid)
            )
            self._layout.addWidget(investigate)

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
        """Build a compact workspace-aware toolbar.

        Phase 11.0 replaces the former flat 20+ action strip with a handful
        of stable workspace menus.  QAction instances are preserved so the
        existing handlers, busy-state logic, shortcuts, and tests keep using
        exactly the same command objects.
        """
        tb = QToolBar()
        tb.setObjectName("MainWorkspaceToolbar")
        tb.setMovable(False)
        self.addToolBar(tb)

        # --- command actions ------------------------------------------------
        self._act_add = QAction("Add Library…", self)
        self._act_add.triggered.connect(self._on_add_library)

        self._act_libraries = QAction("Libraries…", self)
        self._act_libraries.triggered.connect(self._on_manage_libraries)

        self._act_scan = QAction("Scan", self)
        self._act_scan.triggered.connect(self._on_scan)

        self._act_verify = QAction("Verify", self)
        self._act_verify.triggered.connect(self._on_verify)

        self._act_archive_health = QAction("Archive Health", self)
        self._act_archive_health.triggered.connect(self._on_archive_health)

        self._act_extract = QAction("Extract Metadata", self)
        self._act_extract.triggered.connect(lambda: self._start_metadata(auto=False))

        self._act_timeline = QAction("Timeline", self)
        self._act_timeline.triggered.connect(self._on_timeline)

        self._act_organize = QAction("Albums & Tags…", self)
        self._act_organize.triggered.connect(self._on_organize)

        self._act_albums = QAction("Albums", self)
        self._act_albums.triggered.connect(self._on_album_home)

        self._act_tags = QAction("Tags", self)
        self._act_tags.triggered.connect(self._on_tag_home)

        self._act_org_discovery = QAction("Discover", self)
        self._act_org_discovery.triggered.connect(self._on_organization_discovery)

        self._act_org_suggestions = QAction("Assisted Organisation", self)
        self._act_org_suggestions.triggered.connect(self._on_organization_suggestions)

        self._act_org_health = QAction("Organisation Health", self)
        self._act_org_health.triggered.connect(self._on_organization_health)

        self._act_org_activity = QAction("Organisation Activity", self)
        self._act_org_activity.triggered.connect(self._on_organization_activity)

        self._act_org_report = QAction("Export Organisation Report…", self)
        self._act_org_report.triggered.connect(self._on_organization_report)

        self._act_duplicates_lineage = QAction("Duplicates & Lineage", self)
        self._act_duplicates_lineage.triggered.connect(self._on_duplicates_lineage)

        self._act_family_history = QAction("Family History", self)
        self._act_family_history.triggered.connect(self._on_family_history)

        self._act_date_review = QAction("Date Review", self)
        self._act_date_review.triggered.connect(self._on_date_review)

        self._act_unresolved = QAction("Unresolved Memories", self)
        self._act_unresolved.triggered.connect(self._on_unresolved_memories)

        self._act_pilot_audit = QAction("Pilot Audit", self)
        self._act_pilot_audit.triggered.connect(self._on_pilot_audit)

        self._act_pilot_session = QAction("Pilot Session…", self)
        self._act_pilot_session.triggered.connect(self._on_pilot_session)

        self._act_activity_log = QAction("Activity Log…", self)
        self._act_activity_log.triggered.connect(self._on_activity_log)

        self._act_activity_runs = QAction("Activity Runs…", self)
        self._act_activity_runs.triggered.connect(self._on_activity_runs)

        self._act_export_diagnostics = QAction("Export Diagnostics…", self)
        self._act_export_diagnostics.triggered.connect(self._on_export_diagnostics)

        self._act_refresh = QAction("Refresh", self)
        self._act_refresh.triggered.connect(self.refresh)

        # Application-owned command labels. Do not use QAction.text() as the
        # canonical label outside Qt menu rendering: on Windows, ``&`` may be
        # interpreted as a mnemonic marker. These strings are the portable
        # names used by the command palette/search layer.
        self._command_display_labels: dict[int, str] = {
            id(self._act_add): "Add Library…",
            id(self._act_libraries): "Libraries…",
            id(self._act_scan): "Scan",
            id(self._act_verify): "Verify",
            id(self._act_archive_health): "Archive Health",
            id(self._act_extract): "Extract Metadata",
            id(self._act_timeline): "Timeline",
            id(self._act_family_history): "Family History",
            id(self._act_date_review): "Date Review",
            id(self._act_unresolved): "Unresolved Memories",
            id(self._act_organize): "Albums & Tags…",
            id(self._act_albums): "Albums",
            id(self._act_tags): "Tags",
            id(self._act_org_discovery): "Discover",
            id(self._act_org_suggestions): "Assisted Organisation",
            id(self._act_org_health): "Organisation Health",
            id(self._act_org_activity): "Organisation Activity",
            id(self._act_org_report): "Export Organisation Report…",
            id(self._act_duplicates_lineage): "Duplicates & Lineage",
            id(self._act_pilot_audit): "Pilot Audit",
            id(self._act_pilot_session): "Pilot Session…",
            id(self._act_activity_log): "Activity Log…",
            id(self._act_activity_runs): "Activity Runs…",
            id(self._act_export_diagnostics): "Export Diagnostics…",
        }

        self._command_descriptions: dict[int, str] = {
            id(self._act_add): "Register another source-photo library without modifying its files.",
            id(self._act_libraries): "Review registered libraries, availability, counts, and safe forget operations.",
            id(self._act_scan): "Scan the current library for new, changed, moved, restored, or missing files.",
            id(self._act_verify): "Re-check catalogue integrity against source files and recorded hashes.",
            id(self._act_archive_health): "Inspect copy coverage, missing copies, health warnings, hard-link inflation, and filesystem-object evidence without overstating backup independence.",
            id(self._act_extract): "Extract revision-bound metadata from catalogue files without rewriting originals.",
            id(self._act_timeline): "Browse photos through the current authoritative and tentative chronology lanes.",
            id(self._act_family_history): "Open the curated family-history view built from durable Events and stories.",
            id(self._act_date_review): "Review questionable dates, reconstruction proposals, and evidence traces.",
            id(self._act_unresolved): "Show memories that still lack sufficiently reliable chronological placement.",
            id(self._act_organize): "Bulk-curate Albums and Tags for selected logical Photos.",
            id(self._act_albums): "Browse and manage Album homes, covers, membership, and presentation order.",
            id(self._act_tags): "Browse Tags and explicit tag intersections across logical Photos.",
            id(self._act_org_discovery): "Find Photos through explicit Album and Tag intersections.",
            id(self._act_org_suggestions): "Review conservative Album/Event peer-group Tag suggestions before applying them.",
            id(self._act_org_health): "Inspect unorganised Photos, empty Albums, unused Tags, and broken saved views.",
            id(self._act_org_activity): "Review recent organisation changes and safely undo eligible membership edits.",
            id(self._act_org_report): "Export a sanitized organisation-health and activity report.",
            id(self._act_duplicates_lineage): "Investigate exact copies, identity divergence, lineage, and identity-resolution history.",
            id(self._act_pilot_audit): "Create a read-only pilot audit snapshot for chronology-review progress.",
            id(self._act_pilot_session): "Start, checkpoint, inspect, or close a bounded pilot review session.",
            id(self._act_activity_log): "Inspect the application activity log and operational diagnostics.",
            id(self._act_activity_runs): "Review grouped activity runs and their terminal outcomes.",
            id(self._act_export_diagnostics): "Export sanitized diagnostic information without source-photo content.",
        }
        for action_id, description in self._command_descriptions.items():
            action = next(
                (candidate for candidate in self.findChildren(QAction) if id(candidate) == action_id),
                None,
            )
            if action is not None:
                action.setToolTip(description)
                action.setStatusTip(description)

        # Session-local command recall only: no database, registry, or source state.
        self._recent_palette_labels: list[str] = []

        # --- compact workspace navigation ----------------------------------
        self._workspace_buttons: dict[str, QToolButton] = {}
        self._workspace_menus: dict[str, QMenu] = {}
        self._workspace_menu_actions: dict[str, list[QAction]] = {}
        self._workspace_command_actions: dict[str, list[QAction]] = {}

        self._add_workspace_menu(
            tb,
            "Library",
            [
                self._act_add,
                self._act_libraries,
                None,
                self._act_scan,
                self._act_verify,
                self._act_archive_health,
                self._act_extract,
            ],
        )
        self._add_workspace_menu(
            tb,
            "Timeline",
            [
                self._act_timeline,
                self._act_family_history,
                self._act_date_review,
                self._act_unresolved,
            ],
        )
        self._add_workspace_menu(
            tb,
            "Organisation",
            [
                self._act_organize,
                self._act_albums,
                self._act_tags,
                self._act_org_discovery,
                None,
                self._act_org_suggestions,
                self._act_org_health,
                self._act_org_activity,
                None,
                self._act_org_report,
            ],
        )
        self._add_workspace_menu(
            tb,
            "Identity",
            [self._act_duplicates_lineage],
        )
        self._add_workspace_menu(
            tb,
            "Diagnostics",
            [
                self._act_pilot_audit,
                self._act_pilot_session,
                None,
                self._act_activity_log,
                self._act_activity_runs,
                self._act_export_diagnostics,
            ],
        )

        # Keyboard-first navigation. These shortcuts only expose the same
        # workspace menus and canonical command QActions used by mouse input.
        self._workspace_shortcuts: list[QShortcut] = []
        for number, label in enumerate(self._workspace_buttons, start=1):
            shortcut = QShortcut(QKeySequence(f"Alt+{number}"), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(self._workspace_buttons[label].showMenu)
            self._workspace_shortcuts.append(shortcut)
            self._workspace_buttons[label].setToolTip(
                f"{label} workspace (Alt+{number})"
            )

        self._act_command_palette = QAction("Commands…", self)
        self._act_command_palette.setShortcut(QKeySequence("Ctrl+Shift+P"))
        self._act_command_palette.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self._act_command_palette.setToolTip("Command Palette (Ctrl+Shift+P)")
        self._act_command_palette.triggered.connect(self._on_command_palette)

        tb.addSeparator()
        tb.addAction(self._act_command_palette)
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

    def _add_workspace_menu(
        self,
        toolbar: QToolBar,
        label: str,
        actions: list[QAction | None],
    ) -> None:
        """Add one compact workspace button with explicit command proxies.

        Do not insert the command QAction itself into a QMenu.  The command
        action remains the single owner of enabled state/shortcuts/handler
        wiring, while a small menu-local proxy dispatches it explicitly.
        This avoids a platform-specific failure mode where shared QActions can
        render correctly in a tool-button menu yet fail to dispatch reliably.
        """
        menu = QMenu(label, self)
        proxies: list[QAction] = []
        commands: list[QAction] = []
        for command in actions:
            if command is None:
                menu.addSeparator()
                continue

            commands.append(command)
            proxy = QAction(command.icon(), command.text(), menu)
            proxy.setObjectName(f"Workspace{label}Action{len(proxies)}")
            proxy.setToolTip(command.toolTip())
            proxy.setStatusTip(command.statusTip())
            proxy.setEnabled(command.isEnabled())

            # Capture the command by value. QAction.trigger() preserves the
            # established command handler and remains the one dispatch point.
            proxy.triggered.connect(
                lambda _checked=False, target=command: self._dispatch_workspace_command(target)
            )
            command.changed.connect(
                lambda target=command, item=proxy: item.setEnabled(target.isEnabled())
            )
            menu.addAction(proxy)
            proxies.append(proxy)

        button = QToolButton(toolbar)
        button.setObjectName(f"Workspace{label}Button")
        button.setText(label)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(menu)
        button.setToolTip(f"{label} workspace")
        toolbar.addWidget(button)

        self._workspace_buttons[label] = button
        self._workspace_menus[label] = menu
        self._workspace_menu_actions[label] = proxies
        self._workspace_command_actions[label] = commands

    def _dispatch_workspace_command(self, command: QAction) -> None:
        """Dispatch one workspace entry through its canonical QAction.

        A disabled command now gives visible feedback instead of presenting a
        dead menu entry with no explanation.
        """
        if not command.isEnabled():
            self.statusBar().showMessage(
                "Another archive operation is still running; this command will be available when it finishes.",
                5000,
            )
            return
        command.trigger()

    def _palette_commands(self) -> list[PaletteCommand]:
        """Return every canonical workspace command once, in navigation order."""
        commands: list[PaletteCommand] = []
        seen: set[int] = set()
        for workspace, actions in self._workspace_command_actions.items():
            for action in actions:
                if id(action) in seen:
                    continue
                seen.add(id(action))
                label = self._command_display_labels.get(id(action), action.text())
                description = self._command_descriptions.get(id(action), action.toolTip())
                commands.append(PaletteCommand(workspace, action, label, description))
        return commands

    def _remember_palette_command(self, command: PaletteCommand) -> None:
        label = command.label
        self._recent_palette_labels = [item for item in self._recent_palette_labels if item != label]
        self._recent_palette_labels.insert(0, label)
        del self._recent_palette_labels[5:]

    @Slot()
    def _on_command_palette(self) -> None:
        dialog = CommandPaletteDialog(
            self._palette_commands(),
            self,
            recent_labels=self._recent_palette_labels,
            on_command_run=self._remember_palette_command,
        )
        dialog.exec()

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
        self._grid.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self._grid.selectionModel().currentChanged.connect(self._on_selection)
        self._grid.doubleClicked.connect(self._on_open_preview)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            sc = QShortcut(QKeySequence(key), self._grid)
            sc.activated.connect(
                lambda: self._on_open_preview(self._grid.currentIndex()))
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
        self._inspector.investigate_mismatch_requested.connect(self._on_investigate_mismatch)
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
        self._thumb_worker.ready.connect(self._on_thumbnail_ready, Qt.ConnectionType.QueuedConnection)
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
        library_id = self._current_library_id()
        albums = tags = ()
        if library_id is not None:
            try:
                albums = organization.list_photo_albums(self._conn, library_id=library_id, photo_id=detail.photo_id)
                tags = organization.list_photo_tags(self._conn, library_id=library_id, photo_id=detail.photo_id)
            except ValueError:
                pass
        self._inspector.show_detail(detail, thumb, albums=albums, tags=tags)

    def _on_open_preview(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        from ppa.ui.preview_dialog import PreviewDialog
        dialog = PreviewDialog(self._conn, self._model, index.row(), self)
        dialog.show()

    def _current_library_id(self) -> int | None:
        libs = catalogue.list_libraries(self._conn)
        if not libs:
            return None
        if self._current_library is not None:
            try:
                wanted = str(self._current_library.resolve())
            except OSError:
                wanted = str(self._current_library)
            for lib in libs:
                if lib.canonical_path == wanted or lib.display_path == str(self._current_library):
                    return lib.id
        return libs[0].id if len(libs) == 1 else None

    def _selected_photo_ids(self) -> tuple[str, ...]:
        indexes = self._grid.selectionModel().selectedIndexes()
        photo_ids = []
        for index in sorted(indexes, key=lambda i: i.row()):
            item = self._model.item_at(index)
            if item is not None and item.photo_id not in photo_ids:
                photo_ids.append(item.photo_id)
        return tuple(photo_ids)

    def _on_organize(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Albums & Tags", "Select or scan a library first.")
            return
        photo_ids = self._selected_photo_ids()
        from ppa.ui.organization_dialog import OrganizationDialog
        dialog = OrganizationDialog(self._conn, library_id, photo_ids, self)
        dialog.changed.connect(self.refresh)
        dialog.exec()

    def _on_timeline(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Timeline",
                                    "Select or scan a library before opening the timeline.")
            return
        if self._busy:
            return
        self._run_begin("timeline", "timeline", "Timeline requested", {"library_id": library_id})
        self._set_busy(True)
        self._status.showMessage("Timeline: analysing chronology…")
        progress = QProgressDialog("Building chronology timeline…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Timeline")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        self._timeline_progress = progress
        worker = TimelineWorker(self._config.db_path, library_id)
        self._timeline_worker = worker
        worker.progress.connect(self._on_timeline_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_timeline_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_timeline_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._on_timeline_cancelled, Qt.ConnectionType.QueuedConnection)
        progress.canceled.connect(worker.cancel)
        self._registry.start(worker)

    @Slot(str)
    def _on_timeline_progress(self, message: str) -> None:
        self._run_progress("timeline", message)
        self._status.showMessage(message)
        progress = getattr(self, "_timeline_progress", None)
        if progress is not None:
            progress.setLabelText(message)

    def _finish_timeline_progress(self) -> None:
        progress = getattr(self, "_timeline_progress", None)
        if progress is not None:
            progress.close(); progress.deleteLater()
            self._timeline_progress = None
        self._timeline_worker = None
        self._set_busy(False)

    @Slot(object)
    def _on_timeline_ready(self, view) -> None:
        from ppa.ui.timeline_dialog import TimelineDialog
        self._finish_timeline_progress()
        self._run_end("timeline", "success", "Timeline ready", {
            "placed": view.lanes["placed"].count,
            "range": view.lanes["range"].count,
            "tentative": view.lanes["tentative"].count,
            "unplaced": view.lanes["unplaced"].count,
        })
        dialog = TimelineDialog(self._conn, view, self, cache_dir=self._cache_dir)
        dialog.show()
        self._timeline_dialog = dialog
        self._status.showMessage(
            f"Timeline ready — {view.lanes['placed'].count} placed, "
            f"{view.lanes['range'].count} ranges, "
            f"{view.lanes['tentative'].count} tentative, "
            f"{view.lanes['unplaced'].count} unplaced.")

    @Slot()
    def _on_timeline_cancelled(self) -> None:
        self._finish_timeline_progress()
        self._run_end("timeline", "cancelled", "Timeline cancelled")
        self._status.showMessage("Timeline cancelled.")

    @Slot(str)
    def _on_timeline_failed(self, message: str) -> None:
        self._finish_timeline_progress()
        self._run_end("timeline", "failed", f"Timeline failed: {message}")
        self._warn(f"Timeline failed: {message}")
        self._status.showMessage("Timeline failed.")

    def _on_album_home(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Albums", "Select or scan a library before opening Albums.")
            return
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Albums: building visual index…")
        progress = QProgressDialog("Building Album library…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Albums")
        progress.setMinimumDuration(0); progress.setAutoClose(False); progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.show()
        self._album_home_progress = progress
        worker = AlbumHomeWorker(self._config.db_path, library_id)
        self._album_home_worker = worker
        worker.finished.connect(self._on_album_home_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_album_home_failed, Qt.ConnectionType.QueuedConnection)
        progress.canceled.connect(self._cancel_album_home)
        self._registry.start(worker)

    @Slot()
    def _cancel_album_home(self) -> None:
        # Projection is short and read-only; cancellation closes the progress UI
        # and ignores the eventual result rather than terminating SQLite mid-query.
        self._album_home_cancelled = True
        self._finish_album_home_progress()
        self._status.showMessage("Albums cancelled.")

    def _finish_album_home_progress(self) -> None:
        progress = getattr(self, "_album_home_progress", None)
        if progress is not None:
            progress.close(); progress.deleteLater(); self._album_home_progress = None
        self._set_busy(False)

    @Slot(object)
    def _on_album_home_ready(self, home) -> None:
        cancelled = bool(getattr(self, "_album_home_cancelled", False))
        self._album_home_cancelled = False
        self._finish_album_home_progress()
        if cancelled:
            return
        from ppa.ui.album_home_dialog import AlbumHomeDialog
        dialog = AlbumHomeDialog(self._conn, home, self, cache_dir=self._cache_dir / "albums")
        dialog.show(); self._album_home_dialog = dialog
        self._status.showMessage(f"Albums ready — {len(home.cards)} album{'s' if len(home.cards) != 1 else ''}.")

    @Slot(str)
    def _on_album_home_failed(self, message: str) -> None:
        self._album_home_cancelled = False
        self._finish_album_home_progress()
        self._warn(f"Albums failed: {message}")
        self._status.showMessage("Albums failed.")


    def _on_organization_suggestions(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Assisted Organisation", "Select or scan a library first.")
            return
        if self._busy: return
        self._set_busy(True); self._status.showMessage("Assisted Organisation: analysing explicit peer groups…")
        worker = OrganizationSuggestionsWorker(self._config.db_path, library_id)
        self._org_suggestions_worker = worker
        worker.finished.connect(self._on_organization_suggestions_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_organization_suggestions_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_organization_suggestions_ready(self, view) -> None:
        self._set_busy(False)
        from ppa.ui.organization_suggestions_dialog import OrganizationSuggestionsDialog
        dialog = OrganizationSuggestionsDialog(self._conn, self._config.db_path, view, self,
                                               cache_dir=self._cache_dir/'organization-suggestions')
        dialog.show(); self._org_suggestions_dialog = dialog
        self._status.showMessage(f"Assisted Organisation ready — {len(view.suggestions)} suggestion(s).")

    @Slot(str)
    def _on_organization_suggestions_failed(self, message: str) -> None:
        self._set_busy(False); self._warn(f"Assisted Organisation failed: {message}")
        self._status.showMessage("Assisted Organisation failed.")

    def _on_organization_health(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Organisation Health", "Select or scan a library first.")
            return
        if self._busy: return
        self._set_busy(True); self._status.showMessage("Organisation Health: analysing curation gaps…")
        worker = OrganizationHealthWorker(self._config.db_path, library_id)
        self._org_health_worker = worker
        worker.finished.connect(self._on_organization_health_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_organization_health_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_organization_health_ready(self, health) -> None:
        self._set_busy(False)
        from ppa.ui.organization_health_dialog import OrganizationHealthDialog
        dialog = OrganizationHealthDialog(self._conn, self._config.db_path, health, self,
                                          cache_dir=self._cache_dir/'organization-health')
        dialog.show(); self._org_health_dialog = dialog
        self._status.showMessage(f"Organisation Health ready — {health.unorganized_count} unorganised photo(s).")

    @Slot(str)
    def _on_organization_health_failed(self, message: str) -> None:
        self._set_busy(False); self._warn(f"Organisation Health failed: {message}")
        self._status.showMessage("Organisation Health failed.")


    def _on_organization_activity(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Organisation Activity", "Select or scan a library first.")
            return
        if self._busy: return
        self._set_busy(True); self._status.showMessage("Organisation Activity: loading audit history…")
        worker=OrganizationActivityWorker(self._config.db_path,library_id); self._org_activity_worker=worker
        worker.finished.connect(self._on_organization_activity_ready,Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_organization_activity_failed,Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_organization_activity_ready(self, view) -> None:
        self._set_busy(False)
        from ppa.ui.organization_activity_dialog import OrganizationActivityDialog
        d=OrganizationActivityDialog(self._config.db_path,view,self); d.show(); self._org_activity_dialog=d
        self._status.showMessage(f"Organisation Activity ready — {len(view.entries)} recent change(s).")

    @Slot(str)
    def _on_organization_activity_failed(self, message: str) -> None:
        self._set_busy(False); self._warn(f"Organisation Activity failed: {message}"); self._status.showMessage("Organisation Activity failed.")


    def _on_organization_report(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Organisation Report", "Select or scan a library first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Organisation Report", "organisation-report.zip", "ZIP archive (*.zip)")
        if not path: return
        if not path.lower().endswith('.zip'): path += '.zip'
        if self._busy: return
        self._set_busy(True); self._status.showMessage("Organisation Report: building sanitized export…")
        worker=OrganizationReportWorker(self._config.db_path, library_id, Path(path), config=self._config); self._org_report_worker=worker
        worker.finished.connect(self._on_organization_report_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_organization_report_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_organization_report_ready(self, path) -> None:
        self._set_busy(False); self._status.showMessage(f"Organisation Report exported — {path}")
        QMessageBox.information(self, "Organisation Report", f"Sanitized report exported to:\n{path}")

    @Slot(str)
    def _on_organization_report_failed(self, message: str) -> None:
        self._set_busy(False); self._warn(f"Organisation Report failed: {message}"); self._status.showMessage("Organisation Report failed.")

    def _on_tag_home(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Tags", "Select or scan a library before opening Tags.")
            return
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Tags: building visual index…")
        progress = QProgressDialog("Building Tag library…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Tags")
        progress.setMinimumDuration(0); progress.setAutoClose(False); progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.show()
        self._tag_home_progress = progress
        self._tag_home_cancelled = False
        worker = TagHomeWorker(self._config.db_path, library_id)
        self._tag_home_worker = worker
        worker.finished.connect(self._on_tag_home_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_tag_home_failed, Qt.ConnectionType.QueuedConnection)
        progress.canceled.connect(self._cancel_tag_home)
        self._registry.start(worker)

    @Slot()
    def _cancel_tag_home(self) -> None:
        self._tag_home_cancelled = True
        self._finish_tag_home_progress()
        self._status.showMessage("Tags cancelled.")

    def _finish_tag_home_progress(self) -> None:
        progress = getattr(self, "_tag_home_progress", None)
        if progress is not None:
            progress.close(); progress.deleteLater(); self._tag_home_progress = None
        self._set_busy(False)

    @Slot(object)
    def _on_tag_home_ready(self, home) -> None:
        cancelled = bool(getattr(self, "_tag_home_cancelled", False))
        self._tag_home_cancelled = False
        self._finish_tag_home_progress()
        if cancelled:
            return
        from ppa.ui.tag_home_dialog import TagHomeDialog
        dialog = TagHomeDialog(self._conn, home, self, cache_dir=self._cache_dir / "tags")
        dialog.show(); self._tag_home_dialog = dialog
        self._status.showMessage(f"Tags ready — {len(home.cards)} tag{'s' if len(home.cards) != 1 else ''}.")

    @Slot(str)
    def _on_tag_home_failed(self, message: str) -> None:
        self._tag_home_cancelled = False
        self._finish_tag_home_progress()
        self._warn(f"Tags failed: {message}")
        self._status.showMessage("Tags failed.")

    def _on_organization_discovery(self) -> None:
        library_id=self._current_library_id()
        if library_id is None:
            QMessageBox.information(self,"Organisational Discovery","Select or scan a library first."); return
        if self._busy: return
        self._set_busy(True); self._status.showMessage("Discovery: building Album and Tag selectors…")
        progress=QProgressDialog("Building organisation index…","Cancel",0,0,self); progress.setWindowTitle("Organisational Discovery"); progress.setMinimumDuration(0); progress.setAutoClose(False); progress.setAutoReset(False); progress.setWindowModality(Qt.WindowModality.WindowModal); progress.show(); self._org_discovery_progress=progress; self._org_discovery_cancelled=False
        worker=OrganizationDiscoveryHomeWorker(self._config.db_path,library_id); self._org_discovery_worker=worker
        worker.finished.connect(self._on_organization_discovery_ready,Qt.ConnectionType.QueuedConnection); worker.failed.connect(self._on_organization_discovery_failed,Qt.ConnectionType.QueuedConnection); progress.canceled.connect(self._cancel_organization_discovery); self._registry.start(worker)

    @Slot()
    def _cancel_organization_discovery(self) -> None:
        self._org_discovery_cancelled=True; self._finish_organization_discovery(); self._status.showMessage("Discovery cancelled.")

    def _finish_organization_discovery(self) -> None:
        p=getattr(self,"_org_discovery_progress",None)
        if p is not None: p.close(); p.deleteLater(); self._org_discovery_progress=None
        self._set_busy(False)

    @Slot(object,object)
    def _on_organization_discovery_ready(self,albums,tags) -> None:
        cancelled=bool(getattr(self,"_org_discovery_cancelled",False)); self._org_discovery_cancelled=False; self._finish_organization_discovery()
        if cancelled: return
        from ppa.ui.organization_discovery_dialog import OrganizationDiscoveryDialog
        d=OrganizationDiscoveryDialog(self._config.db_path,albums,tags,self,cache_dir=self._cache_dir/"discovery"); d.show(); self._organization_discovery_dialog=d
        self._status.showMessage(f"Discovery ready — {len(albums.cards)} albums · {len(tags.cards)} tags.")

    @Slot(str)
    def _on_organization_discovery_failed(self,message: str) -> None:
        self._org_discovery_cancelled=False; self._finish_organization_discovery(); self._warn(f"Discovery failed: {message}"); self._status.showMessage("Discovery failed.")

    def _on_duplicates_lineage(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Duplicates & Lineage", "Select or scan a library first.")
            return
        self._set_busy(True)
        self._status.showMessage("Duplicates & Lineage: building identity review…")
        worker = DuplicateLineageReviewWorker(self._config.db_path, library_id)
        worker.finished.connect(lambda payload, lid=library_id: self._on_duplicates_lineage_ready(lid, payload), Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_duplicates_lineage_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(int, object)
    def _on_duplicates_lineage_ready(self, library_id: int, payload) -> None:
        self._set_busy(False)
        view, health = payload
        from ppa.ui.duplicate_lineage_dialog import DuplicateLineageDialog
        dialog = DuplicateLineageDialog(self._conn, library_id, view, self, identity_health=health)
        dialog.exec()
        self._status.showMessage(
            f"Duplicates & Lineage ready — {len(view.sets)} exact-copy group(s), "
            f"{len(view.divergences)} identity divergence(s).")

    @Slot(str)
    def _on_duplicates_lineage_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Duplicates & Lineage failed: {message}")
        self._status.showMessage("Duplicates & Lineage failed.")

    def _on_family_history(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Family History",
                                    "Select or scan a library before opening Family History.")
            return
        if self._busy:
            return
        self._run_begin("family_history", "family_history", "Family History requested", {"library_id": library_id})
        self._set_busy(True)
        self._status.showMessage("Family History: building Event index…")
        progress = QProgressDialog("Building Family History…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Family History")
        progress.setMinimumDuration(0); progress.setAutoClose(False); progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.show()
        self._family_history_progress = progress
        worker = EventHomeWorker(self._config.db_path, library_id)
        self._family_history_worker = worker
        worker.progress.connect(self._on_family_history_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_family_history_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_family_history_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._on_family_history_cancelled, Qt.ConnectionType.QueuedConnection)
        progress.canceled.connect(worker.cancel)
        self._registry.start(worker)

    @Slot(str)
    def _on_family_history_progress(self, message: str) -> None:
        self._run_progress("family_history", message)
        self._status.showMessage(message)
        progress = getattr(self, "_family_history_progress", None)
        if progress is not None:
            progress.setLabelText(message)

    def _finish_family_history_progress(self) -> None:
        progress = getattr(self, "_family_history_progress", None)
        if progress is not None:
            progress.close(); progress.deleteLater(); self._family_history_progress = None
        self._family_history_worker = None
        self._set_busy(False)

    @Slot(object, object, object, object)
    def _on_family_history_ready(self, view, home, search_index, health_view) -> None:
        from ppa.ui.event_home_dialog import EventHomeDialog
        self._finish_family_history_progress()
        self._run_end("family_history", "success", "Family History ready", {"events": len(home.cards)})
        dialog = EventHomeDialog(self._conn, view, home, search_index, health_view, self, cache_dir=self._cache_dir / "family-history")
        dialog.show(); self._family_history_dialog = dialog
        self._status.showMessage(f"Family History ready — {len(home.cards)} named event{'s' if len(home.cards) != 1 else ''}.")

    @Slot()
    def _on_family_history_cancelled(self) -> None:
        self._finish_family_history_progress()
        self._run_end("family_history", "cancelled", "Family History cancelled")
        self._status.showMessage("Family History cancelled.")

    @Slot(str)
    def _on_family_history_failed(self, message: str) -> None:
        self._finish_family_history_progress()
        self._run_end("family_history", "failed", f"Family History failed: {message}")
        self._warn(f"Family History failed: {message}")
        self._status.showMessage("Family History failed.")

    def _on_date_review(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Date Review",
                                    "Select or scan a library before starting date review.")
            return
        self._start_date_review_scope(library_id)

    def _start_date_review_scope(self, library_id: int, directory_prefix=None, file_ids=None) -> None:
        """Build a Phase-7 review queue for an explicit, already-validated scope."""
        if self._busy:
            return
        self._run_begin("date_review", "date_review", "Date Review requested", {"library_id": library_id, "directory": directory_prefix, "explicit_files": None if file_ids is None else len(file_ids)})
        self._set_busy(True)
        self._status.showMessage("Date Review: preparing analysis…")
        progress = QProgressDialog("Preparing Date Review…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Date Review")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        self._date_review_progress = progress

        worker = DateReviewQueueWorker(self._config.db_path, library_id, directory_prefix=directory_prefix, file_ids=file_ids)
        self._date_review_worker = worker
        worker.progress.connect(self._on_date_review_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_date_review_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_date_review_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._on_date_review_cancelled, Qt.ConnectionType.QueuedConnection)
        progress.canceled.connect(worker.cancel)
        self._registry.start(worker)

    @Slot(str)
    def _on_date_review_progress(self, message: str) -> None:
        self._run_progress("date_review", message)
        self._status.showMessage(message)
        progress = getattr(self, "_date_review_progress", None)
        if progress is not None:
            progress.setLabelText(message)

    def _finish_date_review_progress(self) -> None:
        progress = getattr(self, "_date_review_progress", None)
        if progress is not None:
            progress.close()
            progress.deleteLater()
            self._date_review_progress = None
        self._date_review_worker = None
        self._set_busy(False)

    @Slot(object)
    def _on_date_review_ready(self, queue) -> None:
        from ppa.ui.preview_dialog import PreviewDialog

        self._finish_date_review_progress()
        items = queue.actionable()
        self._run_end("date_review", "success", "Date Review ready", {"actionable_items": len(items)})
        if not items:
            self._status.showMessage("Date Review: no actionable items.")
            QMessageBox.information(self, "Date Review",
                                    "No actionable date-review items in this library.")
            return
        ids = [i.file_id for i in items]
        qmodel = PhotoGridModel()
        qmodel.set_items(catalogue.grid_items_for_files(self._conn, ids))
        notes = {}
        for i in items:
            prefix = "Best date question" if i.action == "HIGH_LEVERAGE_ANCHOR" else f"Priority {i.priority}"
            leverage = f" · could help {i.affected_count} other photo(s)" if i.affected_count else ""
            notes[i.file_id] = f"{prefix}{leverage} · {i.reason}"
        dialog = PreviewDialog(self._conn, qmodel, 0, self, review_notes=notes,
                               window_title="Date Review")
        dialog._queue_model = qmodel
        dialog.show()
        best = items[0]
        if best.action == "HIGH_LEVERAGE_ANCHOR":
            self._status.showMessage(
                f"Date Review ready — best question could help {best.affected_count} other photo(s).")
        else:
            self._status.showMessage(
                f"Date Review ready — {len(items)} actionable item(s) prioritised.")

    @Slot()
    def _on_date_review_cancelled(self) -> None:
        self._finish_date_review_progress()
        self._run_end("date_review", "cancelled", "Date Review cancelled")
        self._status.showMessage("Date Review cancelled.")

    @Slot(str)
    def _on_date_review_failed(self, message: str) -> None:
        self._finish_date_review_progress()
        self._run_end("date_review", "failed", f"Date Review failed: {message}")
        self._warn(f"Date Review failed: {message}")
        self._status.showMessage("Date Review failed.")

    def _on_unresolved_memories(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Unresolved Memories",
                                    "Select or scan a library before browsing unresolved memories.")
            return
        self._start_unresolved_scope(library_id)

    def _start_unresolved_scope(self, library_id: int, directory_prefix=None, file_ids=None) -> None:
        """Build the read-only unresolved view for an explicit validated pilot scope."""
        if self._busy:
            return
        self._run_begin("unresolved", "unresolved_memories", "Unresolved Memories requested", {"library_id": library_id, "directory": directory_prefix, "explicit_files": None if file_ids is None else len(file_ids)})
        self._set_busy(True)
        self._status.showMessage("Unresolved Memories: analysing…")
        progress = QProgressDialog("Analysing unresolved memories…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Unresolved Memories")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        self._unresolved_progress = progress
        worker = UnresolvedMemoriesWorker(self._config.db_path, library_id, directory_prefix=directory_prefix, file_ids=file_ids)
        self._unresolved_worker = worker
        worker.progress.connect(self._on_unresolved_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_unresolved_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_unresolved_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._on_unresolved_cancelled, Qt.ConnectionType.QueuedConnection)
        progress.canceled.connect(worker.cancel)
        self._registry.start(worker)

    @Slot(str)
    def _on_unresolved_progress(self, message: str) -> None:
        self._run_progress("unresolved", message)
        self._status.showMessage(message)
        progress = getattr(self, "_unresolved_progress", None)
        if progress is not None:
            progress.setLabelText(message)

    def _finish_unresolved_progress(self) -> None:
        progress = getattr(self, "_unresolved_progress", None)
        if progress is not None:
            progress.close(); progress.deleteLater()
            self._unresolved_progress = None
        self._unresolved_worker = None
        self._set_busy(False)

    @Slot(object)
    def _on_unresolved_ready(self, view) -> None:
        from ppa.ui.preview_dialog import PreviewDialog
        self._finish_unresolved_progress()
        self._run_end("unresolved", "success", "Unresolved Memories ready", {"unresolved_items": len(view.items)})
        if not view.items:
            self._status.showMessage("Unresolved Memories: nothing unresolved.")
            QMessageBox.information(self, "Unresolved Memories",
                                    "No unresolved date memories in this library.")
            return
        ids = [i.file_id for i in view.items]
        model = PhotoGridModel()
        model.set_items(catalogue.grid_items_for_files(self._conn, ids))
        notes = {i.file_id: f"{i.label} · {i.reason}" for i in view.items}
        dialog = PreviewDialog(self._conn, model, 0, self, review_notes=notes,
                               window_title="Unresolved Memories")
        dialog._queue_model = model
        dialog.show()
        summary = ", ".join(f"{c.count} {c.label.lower()}" for c in view.categories[:3])
        self._status.showMessage(
            f"Unresolved Memories — {view.unresolved_count} photo(s) intentionally unresolved"
            + (f" · {summary}" if summary else ""))

    @Slot()
    def _on_unresolved_cancelled(self) -> None:
        self._finish_unresolved_progress()
        self._run_end("unresolved", "cancelled", "Unresolved Memories cancelled")
        self._status.showMessage("Unresolved Memories cancelled.")

    @Slot(str)
    def _on_unresolved_failed(self, message: str) -> None:
        self._finish_unresolved_progress()
        self._run_end("unresolved", "failed", f"Unresolved Memories failed: {message}")
        self._warn(f"Unresolved Memories failed: {message}")
        self._status.showMessage("Unresolved Memories failed.")

    def _on_pilot_session(self) -> None:
        if self._busy:
            return
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Pilot Session",
                                    "Select or scan a library before starting a pilot session.")
            return
        from ppa.ui.pilot_dashboard_dialog import PilotDashboardDialog
        dialog = PilotDashboardDialog(self._config, library_id, self._registry, self)
        dialog.request_date_review.connect(self._start_date_review_scope)
        dialog.request_unresolved.connect(self._start_unresolved_scope)
        dialog.show()
        self._pilot_dashboard_dialog = dialog


    def _run_begin(self, key: str, operation: str, message: str, detail=None) -> str:
        if not hasattr(self, "_activity_runs"):
            self._activity_runs = {}
        run_id = new_run_id()
        self._activity_runs[key] = (run_id, operation, time.monotonic())
        log.info(message, extra=run_extra(run_id, operation, "start", detail=detail))
        return run_id

    def _run_progress(self, key: str, message: str) -> None:
        item = getattr(self, "_activity_runs", {}).get(key)
        if item:
            run_id, operation, _started = item
            log.info(message, extra=run_extra(run_id, operation, "progress"))

    def _run_end(self, key: str, outcome: str, message: str, detail=None) -> None:
        item = getattr(self, "_activity_runs", {}).pop(key, None)
        if item:
            run_id, operation, started = item
            elapsed = int((time.monotonic() - started) * 1000)
            level = log.error if outcome == "failed" else log.info
            level(message, extra=run_extra(run_id, operation, "end", outcome=outcome, elapsed_ms=elapsed, detail=detail))

    def _on_activity_runs(self) -> None:
        from ppa.ui.runs_dialog import RunsDialog
        dialog = RunsDialog(self._config, self)
        dialog.show()
        self._activity_runs_dialog = dialog

    def _on_activity_log(self) -> None:
        """Open the live, auto-refreshing human-readable operational log."""
        from ppa.ui.log_dialog import LogDialog
        log.info("Activity Log opened")
        dialog = LogDialog(self._config, self)
        dialog.show()
        self._activity_log_dialog = dialog

    def _on_export_diagnostics(self) -> None:
        """Create a sanitized shareable diagnostics ZIP; never include photos/DB."""
        from datetime import datetime
        from ppa.diagnostics import export_diagnostics
        default = f"ppa-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export shareable diagnostics", default, "ZIP files (*.zip)")
        if not filename:
            return
        try:
            path = export_diagnostics(self._config, Path(filename))
            log.info("Sanitized diagnostics exported to %s", path)
            QMessageBox.information(
                self, "Diagnostics exported",
                f"Created:\n{path}\n\nNo catalogue database or photo files are included.")
        except Exception as exc:
            log.exception("Diagnostics export failed")
            self._warn(f"Diagnostics export failed: {exc}")

    def _on_pilot_audit(self) -> None:
        """Build the read-only Phase-7 audit snapshot off-thread."""
        if self._busy:
            return
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Pilot Audit",
                                    "Select or scan a library before running the audit.")
            return
        self._run_begin("pilot_audit", "pilot_audit", "Pilot Audit requested", {"library_id": library_id})
        self._set_busy(True)
        self._status.showMessage("Pilot Audit: analysing…")
        progress = QProgressDialog("Building Phase 7 pilot audit…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Pilot Audit")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        self._pilot_audit_progress = progress
        worker = PilotAuditWorker(self._config.db_path, library_id)
        self._pilot_audit_worker = worker
        worker.progress.connect(self._on_pilot_audit_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_pilot_audit_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_pilot_audit_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._on_pilot_audit_cancelled, Qt.ConnectionType.QueuedConnection)
        progress.canceled.connect(worker.cancel)
        self._registry.start(worker)

    @Slot(str)
    def _on_pilot_audit_progress(self, message: str) -> None:
        self._run_progress("pilot_audit", message)
        self._status.showMessage(message)
        progress = getattr(self, "_pilot_audit_progress", None)
        if progress is not None:
            progress.setLabelText(message)

    def _finish_pilot_audit_progress(self) -> None:
        progress = getattr(self, "_pilot_audit_progress", None)
        if progress is not None:
            progress.close(); progress.deleteLater()
            self._pilot_audit_progress = None
        self._pilot_audit_worker = None
        self._set_busy(False)

    @Slot(object)
    def _on_pilot_audit_ready(self, snapshot) -> None:
        from ppa.pilot_audit import concise_text
        self._finish_pilot_audit_progress()
        self._run_end("pilot_audit", "success", "Pilot Audit ready", {"usable": snapshot.usable_chronology.count, "unresolved": snapshot.unresolved.count, "stale": snapshot.stale_decisions.count})
        text = concise_text(snapshot)
        box = QMessageBox(self)
        box.setWindowTitle("Phase 7 Pilot Audit")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("Phase 7 pilot audit complete")
        box.setInformativeText(
            f"Usable chronology: {snapshot.usable_chronology.count} / {snapshot.total_files}\n"
            f"Confirmed current: {snapshot.confirmed_current.count}\n"
            f"Unresolved: {snapshot.unresolved.count}\n"
            f"Stale decisions: {snapshot.stale_decisions.count}")
        box.setDetailedText(text)
        box.exec()
        self._status.showMessage(
            f"Pilot Audit — {snapshot.usable_chronology.count}/{snapshot.total_files} usable; "
            f"{snapshot.unresolved.count} unresolved")

    @Slot()
    def _on_pilot_audit_cancelled(self) -> None:
        self._finish_pilot_audit_progress()
        self._run_end("pilot_audit", "cancelled", "Pilot Audit cancelled")
        self._status.showMessage("Pilot Audit cancelled.")

    @Slot(str)
    def _on_pilot_audit_failed(self, message: str) -> None:
        self._finish_pilot_audit_progress()
        self._run_end("pilot_audit", "failed", f"Pilot Audit failed: {message}")
        self._warn(f"Pilot Audit failed: {message}")
        self._status.showMessage("Pilot Audit failed.")

    # --- thumbnails ---------------------------------------------------------
    @Slot(str, object)
    def _on_thumbnail_ready(self, file_id: str, image) -> None:
        self._model.set_thumbnail(file_id, QPixmap.fromImage(image))

    # --- scan / verify ------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for act in (self._act_add, self._act_libraries, self._act_scan, self._act_verify,
                    self._act_archive_health, self._act_extract, self._act_date_review, self._act_unresolved,
                    self._act_pilot_audit, self._act_pilot_session, self._act_refresh):
            act.setEnabled(not busy)

    def _on_add_library(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a photo library")
        if not directory:
            return
        self._current_library = Path(directory)
        self._update_status_summary()
        if self._confirm(f"Scan {directory} now?"):
            self._on_scan()

    def _on_manage_libraries(self) -> None:
        if self._busy:
            return
        from ppa.ui.libraries_dialog import LibrariesDialog
        dialog = LibrariesDialog(self._conn, self._current_library, self)
        dialog.exec()
        if dialog.target_request is not None:
            self._current_library = dialog.target_request
        # A library may have been removed inside the dialog; reflect it.
        self.refresh()
        self._update_status_summary()
        if dialog.scan_request is not None:
            self._current_library = dialog.scan_request
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

        self._run_begin("scan", "scan", "Scan requested", {"library": str(self._current_library)})
        self._set_busy(True)
        self._status.showMessage("Starting scan…")
        data_dir = self._config.db_path.parent
        protected = [self._config.db_path, data_dir / "thumbnails", self._config.log_path]
        worker = ScanWorker(
            self._config.db_path, self._current_library, protected_paths=protected
        )
        worker.progress.connect(self._on_scan_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_scan_done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_scan_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(str)
    def _on_scan_progress(self, message: str) -> None:
        self._run_progress("scan", message)
        self._status.showMessage(message)

    @Slot(str)
    def _on_scan_failed(self, message: str) -> None:
        self._run_end("scan", "failed", f"Scan failed: {message}")
        self._on_worker_failed(message)

    @Slot(object)
    def _on_scan_done(self, report) -> None:
        self._run_end("scan", "success", "Scan complete", {"new_files": report.new_files, "moved_files": report.moved_files, "duplicates": report.duplicate_files, "missing": report.missing_files})
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
        worker.progress.connect(self._on_metadata_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_metadata_done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_worker_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(str)
    def _on_metadata_progress(self, message: str) -> None:
        self._status.showMessage(message)

    @Slot(int)
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

    def _on_archive_health(self) -> None:
        library_id = self._current_library_id()
        if library_id is None:
            QMessageBox.information(self, "Backup & Archive Health", "Select or scan a library first.")
            return
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Archive Health: analysing copy coverage and storage identity…")
        worker = ArchiveHealthWorker(self._config.db_path, library_id)
        self._archive_health_worker = worker
        worker.finished.connect(self._on_archive_health_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_archive_health_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_archive_health_ready(self, health) -> None:
        self._set_busy(False)
        from ppa.ui.archive_health_dialog import ArchiveHealthDialog
        dialog = ArchiveHealthDialog(
            self._conn, self._config.db_path, health, self,
            cache_dir=self._cache_dir / 'archive-health',
        )
        dialog.show()
        self._archive_health_dialog = dialog
        self._status.showMessage(
            f"Archive Health ready — {health.no_present_count} with no present File, "
            f"{health.single_present_count} with one present File, "
            f"{health.hardlink_overstated_count} exact set(s) with hard-link path inflation."
        )

    @Slot(str)
    def _on_archive_health_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Archive Health failed: {message}")
        self._status.showMessage("Archive Health failed.")

    @Slot(str)
    def _on_investigate_mismatch(self, file_id: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Building hash-mismatch forensic comparison…")
        worker = MismatchInvestigationWorker(
            self._config.db_path, file_id, self._cache_dir
        )
        worker.finished.connect(
            self._on_mismatch_investigation_ready, Qt.ConnectionType.QueuedConnection
        )
        worker.failed.connect(
            self._on_mismatch_investigation_failed, Qt.ConnectionType.QueuedConnection
        )
        self._registry.start(worker)

    @Slot(object)
    def _on_mismatch_investigation_ready(self, investigation) -> None:
        self._set_busy(False)
        from ppa.ui.mismatch_investigation_dialog import MismatchInvestigationDialog
        dialog = MismatchInvestigationDialog(investigation, self)
        dialog.resolution_requested.connect(
            lambda action, note, inv=investigation: self._on_mismatch_resolution_requested(inv, action, note)
        )
        dialog.recovery_planning_requested.connect(self._on_recovery_planning_requested)
        dialog.show()
        self._mismatch_investigation_dialog = dialog
        self._status.showMessage(
            "Mismatch investigation ready — current bytes "
            + investigation.current_state.replace("_", " ")
            + "."
        )

    @Slot(str)
    def _on_mismatch_investigation_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Hash mismatch investigation failed: {message}")
        self._status.showMessage("Hash mismatch investigation failed.")

    @Slot(str)
    def _on_recovery_planning_requested(self, file_id: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Recovery Planning: physically qualifying donor copies…")
        worker = RecoveryPlanningWorker(self._config.db_path, file_id)
        worker.finished.connect(self._on_recovery_planning_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_recovery_planning_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_recovery_planning_ready(self, bundle) -> None:
        self._set_busy(False)
        from ppa.ui.recovery_planning_dialog import RecoveryPlanningDialog
        view, plan = bundle
        dialog = RecoveryPlanningDialog(view, plan, self)
        dialog.proposal_requested.connect(self._on_recovery_proposal_requested)
        dialog.show()
        self._recovery_planning_dialog = dialog
        self._status.showMessage(
            f"Recovery Planning ready — {len(view.qualified_candidates)} qualified donor(s); "
            "dry-run only."
        )

    @Slot(str)
    def _on_recovery_planning_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Recovery planning failed: {message}")
        self._status.showMessage("Recovery planning failed closed; no source file was changed.")

    def _on_recovery_proposal_requested(self, plan, note: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Revalidating and recording dry-run recovery proposal…")
        worker = RecoveryProposalWorker(self._config.db_path, plan, note or None)
        worker.finished.connect(self._on_recovery_proposal_done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_recovery_proposal_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_recovery_proposal_done(self, result) -> None:
        self._set_busy(False)
        self._status.showMessage(
            f"Recovery proposal {result.proposal_id} recorded — proposed but NOT executed."
        )
        answer = QMessageBox.question(
            self,
            "Stage suspect bytes for recovery?",
            "Phase 14.0 can now preserve the currently suspect target bytes into PPA's "
            "operational recovery-preservation store.\n\n"
            "This WILL write a separate preservation copy outside the source Library, but it "
            "WILL NOT replace, rename, move, delete, or otherwise modify the source photograph, "
            "and donor bytes will not be copied.\n\n"
            "Stage preservation now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._on_recovery_preservation_requested(result.proposal_id)

    def _on_recovery_preservation_requested(self, proposal_id: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Revalidating recovery proposal and staging suspect-byte preservation…")
        worker = RecoveryPreservationWorker(self._config.db_path, proposal_id)
        worker.finished.connect(self._on_recovery_preservation_done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_recovery_preservation_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_recovery_preservation_done(self, result) -> None:
        self._set_busy(False)
        if result.preservation_path:
            self._status.showMessage(
                f"Suspect bytes preserved for recovery stage {result.stage_id}; source target was NOT replaced."
            )
            QMessageBox.information(
                self,
                "Recovery preservation staged",
                f"Suspect target bytes were preserved and verified.\n\n"
                f"Preservation: {result.preservation_path}\n"
                f"SHA-256: {result.preserved_sha256}\n\n"
                "The source target was not replaced and donor bytes were not materialized.",
            )
        else:
            self._status.showMessage(
                f"Recovery stage {result.stage_id} recorded: target is missing, so no suspect bytes existed to preserve."
            )
            QMessageBox.information(
                self,
                "Recovery preservation staged",
                "The target remains missing, so there were no suspect target bytes to preserve. "
                "The recovery checkpoint was recorded; no source or donor bytes were written.",
            )

        answer = QMessageBox.question(
            self,
            "Materialize verified donor?",
            "Phase 14.1 can now copy the freshly re-attested expected donor bytes into the "
            "same protected operational recovery stage.\n\n"
            "This WILL create a separate verified donor copy in PPA operational storage. "
            "It WILL NOT replace, create, rename, move, delete, or otherwise modify the source target, "
            "and it will not modify the original donor.\n\n"
            "Materialize the verified donor now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._on_recovery_donor_materialization_requested(result.stage_id)

    def _on_recovery_donor_materialization_requested(self, stage_id: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Revalidating and materializing verified donor bytes into protected staging…")
        worker = RecoveryDonorMaterializationWorker(self._config.db_path, stage_id)
        worker.finished.connect(self._on_recovery_donor_materialization_done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_recovery_donor_materialization_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _on_recovery_donor_materialization_done(self, result) -> None:
        self._set_busy(False)
        self._status.showMessage(
            f"Verified donor materialized for recovery stage {result.stage_id}; source target was NOT replaced."
        )
        QMessageBox.information(
            self,
            "Verified donor materialized",
            f"Expected donor bytes were copied into protected PPA operational storage and verified.\n\n"
            f"Materialized donor: {result.donor_materialization_path}\n"
            f"SHA-256: {result.donor_materialized_sha256}\n\n"
            "The original donor was not modified and the source target was not replaced.",
        )

    @Slot(str)
    def _on_recovery_donor_materialization_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Recovery donor materialization failed: {message}")
        self._status.showMessage("Donor materialization failed closed; source target and donor were not modified.")

    @Slot(str)
    def _on_recovery_preservation_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Recovery preservation staging failed: {message}")
        self._status.showMessage("Recovery preservation failed closed; source and donor were not modified.")

    @Slot(str)
    def _on_recovery_proposal_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Recovery proposal was not recorded: {message}")
        self._status.showMessage("Recovery proposal aborted; evidence changed or was not eligible.")

    def _on_mismatch_resolution_requested(self, investigation, action: str, note: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Revalidating and recording hash-mismatch resolution…")
        worker = MismatchResolutionWorker(
            self._config.db_path, investigation, action, note or None
        )
        worker.finished.connect(
            self._on_mismatch_resolution_done, Qt.ConnectionType.QueuedConnection
        )
        worker.failed.connect(
            self._on_mismatch_resolution_failed, Qt.ConnectionType.QueuedConnection
        )
        self._registry.start(worker)

    @Slot(object)
    def _on_mismatch_resolution_done(self, result) -> None:
        self._set_busy(False)
        self.refresh()
        labels = {
            "adopt_current_revision": "Current bytes adopted as a new immutable revision.",
            "retain_expected_recovery_needed": "Expected revision retained; recovery remains needed.",
            "reviewed_unresolved": "Mismatch review recorded as unresolved.",
        }
        self._status.showMessage(labels.get(result.action, "Mismatch resolution recorded."))

    @Slot(str)
    def _on_mismatch_resolution_failed(self, message: str) -> None:
        self._set_busy(False)
        self._warn(f"Mismatch resolution was not applied: {message}")
        self._status.showMessage("Mismatch resolution aborted; review evidence changed or was not eligible.")

    def _on_verify(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.showMessage("Starting integrity verification…")
        worker = VerifyWorker(self._config.db_path)
        worker.progress.connect(self._on_verify_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_verify_done, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_worker_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(str)
    def _on_verify_progress(self, message: str) -> None:
        self._status.showMessage(message)

    @Slot(object)
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

    @Slot(str)
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
        try:
            self._conn.close()
        finally:
            super().closeEvent(event)
