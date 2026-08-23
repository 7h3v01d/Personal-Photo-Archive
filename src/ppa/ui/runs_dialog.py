"""Read-only viewer for correlated operational runs."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMessageBox, QPushButton, QPlainTextEdit, QSplitter, QVBoxLayout, QWidget

from ppa.activity_runs import export_run_transcript, load_activity_runs
from ppa.logging_setup import get_logger

log = get_logger("diagnostics.runs")

class RunsDialog(QDialog):
    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._runs = []
        self.setWindowTitle("PPA Activity Runs")
        self.resize(1050, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Correlated operational runs from the structured log. Diagnostics only; never archive evidence."))
        split = QSplitter()
        self._list = QListWidget(); self._detail = QPlainTextEdit(); self._detail.setReadOnly(True)
        self._list.currentRowChanged.connect(self._show_selected)
        split.addWidget(self._list); split.addWidget(self._detail); split.setSizes([380, 670])
        layout.addWidget(split, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh"); refresh.clicked.connect(self.refresh)
        export = QPushButton("Export selected run…"); export.clicked.connect(self._export_selected)
        close = QPushButton("Close"); close.clicked.connect(self.accept)
        row.addWidget(refresh); row.addWidget(export); row.addStretch(1); row.addWidget(close)
        layout.addLayout(row)
        self._timer = QTimer(self); self._timer.setInterval(1500); self._timer.timeout.connect(self.refresh); self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        current_id = None
        row = self._list.currentRow()
        if 0 <= row < len(self._runs): current_id = self._runs[row].run_id
        self._runs = list(load_activity_runs(self._config.log_path, limit=200))
        self._list.blockSignals(True); self._list.clear()
        select = -1
        for idx, run in enumerate(self._runs):
            dur = "" if run.elapsed_ms is None else f" · {run.elapsed_ms/1000:.1f}s"
            self._list.addItem(f"{run.operation} · {run.outcome}{dur}\n{run.started_at} · {run.run_id}")
            if run.run_id == current_id: select = idx
        self._list.blockSignals(False)
        if self._runs:
            self._list.setCurrentRow(select if select >= 0 else 0)
        else:
            self._detail.setPlainText("No correlated runs yet. New Phase-7.6 operations will appear here.")

    def _show_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._runs): return
        r = self._runs[row]
        lines = [f"Run: {r.run_id}", f"Operation: {r.operation}", f"Outcome: {r.outcome}", f"Started: {r.started_at}", f"Ended: {r.ended_at or 'still running'}", f"Duration: {'-' if r.elapsed_ms is None else str(r.elapsed_ms) + ' ms'}", "", "Events"]
        for e in r.events:
            detail = f" · {e.detail}" if e.detail else ""
            lines.append(f"{e.timestamp}  {e.phase.upper():8s}  {e.message}{detail}")
        self._detail.setPlainText("\n".join(lines))

    def _export_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._runs): return
        run = self._runs[row]
        default = f"ppa-run-{run.operation}-{run.run_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        filename, _ = QFileDialog.getSaveFileName(self, "Export sanitized run transcript", default, "JSON files (*.json)")
        if not filename: return
        try:
            path = export_run_transcript(self._config, run.run_id, Path(filename))
            log.info("Run transcript exported to %s", path)
            QMessageBox.information(self, "Run exported", f"Created:\n{path}\n\nNo database or photo files are included.")
        except Exception as exc:
            log.exception("Run transcript export failed")
            QMessageBox.warning(self, "Export failed", str(exc))
