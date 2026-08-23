"""Live operational-log viewer and sanitized diagnostics export."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QPlainTextEdit, QVBoxLayout,
)

from ppa.diagnostics import export_diagnostics, tail_text
from ppa.logging_setup import get_logger

log = get_logger("diagnostics")


class LogDialog(QDialog):
    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("PPA Activity Log")
        self.resize(980, 620)

        layout = QVBoxLayout(self)
        location = QLabel(f"Live log: {config.log_path}")
        location.setTextInteractionFlags(location.textInteractionFlags())
        layout.addWidget(location)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._text, 1)

        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        folder = QPushButton("Open log folder")
        folder.clicked.connect(self._open_folder)
        export = QPushButton("Export diagnostics…")
        export.clicked.connect(self._export)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(refresh); row.addWidget(folder); row.addWidget(export)
        row.addStretch(1); row.addWidget(close)
        layout.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        text = tail_text(self._config.log_path, lines=700)
        current = self._text.toPlainText()
        if text == current:
            return
        at_bottom = self._text.verticalScrollBar().value() >= self._text.verticalScrollBar().maximum() - 2
        self._text.setPlainText(text or "No log entries yet.")
        if at_bottom:
            self._text.verticalScrollBar().setValue(self._text.verticalScrollBar().maximum())

    def _open_folder(self) -> None:
        self._config.log_path.parent.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._config.log_path.parent)))

    def _export(self) -> None:
        default = f"ppa-diagnostics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        filename, _ = QFileDialog.getSaveFileName(self, "Export shareable diagnostics", default, "ZIP files (*.zip)")
        if not filename:
            return
        try:
            path = export_diagnostics(self._config, Path(filename))
            log.info("Sanitized diagnostics exported to %s", path)
            QMessageBox.information(self, "Diagnostics exported", f"Created:\n{path}\n\nNo catalogue database or photo files are included.")
        except Exception as exc:
            log.exception("Diagnostics export failed")
            QMessageBox.warning(self, "Export failed", str(exc))
