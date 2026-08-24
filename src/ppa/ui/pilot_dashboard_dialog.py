"""Phase 7.4 desktop pilot-session dashboard."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal, Qt, Slot
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QMessageBox, QProgressDialog, QPushButton, QTextEdit, QVBoxLayout,
)

from ppa.pilot_dashboard import build_dashboard_view
from ppa.pilot_session import load_pilot_session
from ppa.ui.workers import PilotSessionWorker, ReviewProgressExportWorker


class PilotDashboardDialog(QDialog):
    """Orchestrates an external Phase-7 pilot session without owning chronology logic."""

    request_date_review = Signal(int, object, object)
    request_unresolved = Signal(int, object, object)

    def __init__(self, config, library_id: int, registry, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._db_path = Path(config.db_path)
        self._library_id = library_id
        self._registry = registry
        self._session_path: Path | None = None
        self._session = None
        self._current = None
        self._progress = None
        self._worker = None

        self.setWindowTitle("Phase 7 Pilot Session")
        self.resize(720, 590)
        self._build_ui()
        self._render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel("Phase 7 · Real-Collection Pilot")
        title.setObjectName("InspectorTitle")
        root.addWidget(title)
        hint = QLabel(
            "Start or resume one integrity-checked pilot session, measure progress against its "
            "immutable baseline, and launch review tools inside that exact saved scope."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        top = QHBoxLayout()
        self._start = QPushButton("Start new…")
        self._load = QPushButton("Load session…")
        self._refresh = QPushButton("Refresh current")
        self._start.clicked.connect(self._start_new)
        self._load.clicked.connect(self._load_existing)
        self._refresh.clicked.connect(lambda: self._run("refresh"))
        top.addWidget(self._start); top.addWidget(self._load); top.addWidget(self._refresh)
        top.addStretch(1)
        root.addLayout(top)

        self._summary = QTextEdit()
        self._summary.setReadOnly(True)
        root.addWidget(self._summary, 1)

        actions = QGridLayout()
        self._date = QPushButton("Continue Date Review")
        self._unresolved = QPushButton("Browse Unresolved Memories")
        self._checkpoint = QPushButton("Capture checkpoint…")
        self._close_pilot = QPushButton("Close pilot")
        self._share = QPushButton("Share progress…")
        self._date.clicked.connect(self._launch_date_review)
        self._unresolved.clicked.connect(self._launch_unresolved)
        self._checkpoint.clicked.connect(self._checkpoint_now)
        self._close_pilot.clicked.connect(self._close_now)
        self._share.clicked.connect(self._share_progress)
        actions.addWidget(self._date, 0, 0)
        actions.addWidget(self._unresolved, 0, 1)
        actions.addWidget(self._checkpoint, 1, 0)
        actions.addWidget(self._close_pilot, 1, 1)
        actions.addWidget(self._share, 2, 0, 1, 2)
        root.addLayout(actions)

        bottom = QHBoxLayout(); bottom.addStretch(1)
        dismiss = QPushButton("Done"); dismiss.clicked.connect(self.accept)
        bottom.addWidget(dismiss); root.addLayout(bottom)

    def _render(self) -> None:
        if self._session is None:
            self._summary.setPlainText(
                "No pilot session loaded.\n\nStart a new session to capture an immutable baseline, "
                "or load an existing .json pilot artifact."
            )
            for w in (self._refresh, self._date, self._unresolved, self._checkpoint, self._close_pilot, self._share):
                w.setEnabled(False)
            return

        view = build_dashboard_view(self._session, self._current)
        lines = [
            f"Session: {view.session_id}",
            f"Status: {view.status.upper()}",
            f"Library: {view.library_root}",
            f"Scope: {view.scope_label}",
            f"Checkpoints: {view.checkpoint_count}",
            f"Artifact: {self._session_path}", "",
        ]
        if view.current_available:
            lines += ["BASELINE → CURRENT", "------------------"]
            for m in view.metrics:
                sign = "+" if m.delta > 0 else ""
                lines.append(f"{m.label:20} {m.baseline:6} → {m.current:6}  ({sign}{m.delta})")
            lines.append("")
        else:
            lines += ["Current catalogue state has not yet been validated against this session.", ""]
        lines += ["NEXT", "----", view.suggested_action, "", "Session/source-photo writes: 0"]
        self._summary.setPlainText("\n".join(lines))

        open_session = self._session.status == "open"
        validated = self._current is not None
        self._refresh.setEnabled(open_session)
        self._date.setEnabled(open_session and validated)
        self._unresolved.setEnabled(open_session and validated)
        self._checkpoint.setEnabled(open_session and validated)
        self._close_pilot.setEnabled(open_session and validated)
        self._share.setEnabled(validated or (self._session.status == "closed" and self._session.final is not None))

    def _start_new(self) -> None:
        default_dir = self._db_path.parent / "pilots"
        default_dir.mkdir(parents=True, exist_ok=True)
        filename, _ = QFileDialog.getSaveFileName(
            self, "Create pilot session", str(default_dir / f"pilot-library-{self._library_id}.json"),
            "PPA Pilot Session (*.json)")
        if not filename:
            return
        prefix, ok = QInputDialog.getText(
            self, "Pilot scope", "Optional directory prefix inside the library\n(blank = entire library):")
        if not ok:
            return
        self._session_path = Path(filename)
        self._run("start", directory_prefix=prefix.strip() or None)

    def _load_existing(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load pilot session", str(self._db_path.parent / "pilots"),
            "PPA Pilot Session (*.json);;JSON (*.json)")
        if not filename:
            return
        try:
            session = load_pilot_session(Path(filename))
        except Exception as exc:
            QMessageBox.critical(self, "Pilot Session", f"Could not load session:\n{exc}")
            return
        self._session_path = Path(filename)
        self._session = session
        self._current = session.final if session.status == "closed" else None
        self._render()
        if session.status == "open":
            self._run("refresh")

    def _run(self, operation: str, *, directory_prefix: str | None = None,
             label: str | None = None) -> None:
        if operation != "start" and (self._session_path is None or self._session is None):
            return
        self._set_controls(False)
        progress = QProgressDialog("Preparing pilot operation…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Pilot Session")
        progress.setMinimumDuration(0); progress.setAutoClose(False); progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.show()
        self._progress = progress
        worker = PilotSessionWorker(
            self._db_path, operation, self._session_path,
            library_id=self._library_id if operation == "start" else None,
            directory_prefix=directory_prefix, label=label)
        self._worker = worker
        worker.progress.connect(self._operation_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._operation_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._operation_failed, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(self._operation_cancelled, Qt.ConnectionType.QueuedConnection)
        progress.canceled.connect(worker.cancel)
        self._registry.start(worker)

    @Slot(str)
    def _operation_progress(self, message: str) -> None:
        if self._progress is not None:
            self._progress.setLabelText(message)

    def _finish_progress(self) -> None:
        if self._progress is not None:
            self._progress.close(); self._progress.deleteLater(); self._progress = None
        self._worker = None
        self._set_controls(True)

    def _set_controls(self, enabled: bool) -> None:
        self._start.setEnabled(enabled); self._load.setEnabled(enabled)
        if enabled:
            self._render()
        else:
            for w in (self._refresh, self._date, self._unresolved, self._checkpoint, self._close_pilot, self._share):
                w.setEnabled(False)

    @Slot(object, object)
    def _operation_ready(self, session, current) -> None:
        self._finish_progress()
        self._session = session; self._current = current
        self._render()

    @Slot(str)
    def _operation_failed(self, message: str) -> None:
        self._finish_progress()
        QMessageBox.critical(self, "Pilot Session", message)
        self._render()

    @Slot()
    def _operation_cancelled(self) -> None:
        self._finish_progress(); self._render()

    def _checkpoint_now(self) -> None:
        label, ok = QInputDialog.getText(self, "Pilot checkpoint", "Checkpoint label:")
        if ok:
            self._run("checkpoint", label=label.strip() or None)

    def _close_now(self) -> None:
        answer = QMessageBox.question(
            self, "Close pilot",
            "Capture the final audit and permanently close this pilot session?\n\n"
            "The baseline and checkpoints will be preserved in the session artifact.")
        if answer == QMessageBox.StandardButton.Yes:
            self._run("close")


    def _share_progress(self) -> None:
        if self._session is None:
            return
        current = self._current if self._current is not None else self._session.final
        if current is None:
            QMessageBox.information(self, "Share progress",
                                    "Refresh the pilot first so its current scope is validated.")
            return
        default_dir = self._db_path.parent / "reports"
        default_dir.mkdir(parents=True, exist_ok=True)
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export shareable progress report",
            str(default_dir / f"ppa-review-progress-{self._session.session_id[:8]}.zip"),
            "ZIP files (*.zip)")
        if not filename:
            return
        self._set_controls(False)
        progress = QProgressDialog("Building shareable progress report…", "", 0, 0, self)
        progress.setWindowTitle("Share progress")
        progress.setMinimumDuration(0); progress.setAutoClose(False); progress.setAutoReset(False)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.show()
        self._progress = progress
        worker = ReviewProgressExportWorker(self._config, self._session, current, Path(filename))
        self._worker = worker
        worker.finished.connect(self._share_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._share_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _share_ready(self, path) -> None:
        self._finish_progress()
        QMessageBox.information(
            self, "Progress report exported",
            f"Created:\n{path}\n\nThe bundle contains aggregate progress and scoped run summaries only; "
            "no photos, catalogue database, raw paths, photo IDs, or raw log messages are included.")

    @Slot(str)
    def _share_failed(self, message: str) -> None:
        self._finish_progress()
        QMessageBox.critical(self, "Share progress", f"Could not export report:\n{message}")

    def _launch_date_review(self) -> None:
        s = self._session
        if s is not None and self._current is not None:
            self.request_date_review.emit(s.library_id, s.directory_prefix, s.explicit_file_ids)

    def _launch_unresolved(self) -> None:
        s = self._session
        if s is not None and self._current is not None:
            self.request_unresolved.emit(s.library_id, s.directory_prefix, s.explicit_file_ids)
