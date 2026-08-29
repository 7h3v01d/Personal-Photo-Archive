"""Phase 12.3 — explicit expected-vs-current hash-mismatch investigation UI."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget, QMessageBox, QInputDialog,
)

from ppa.ui import theme


def _short(value: str | None, n: int = 18) -> str:
    if not value:
        return "unavailable"
    return value if len(value) <= n else value[:n] + "…"


class MismatchInvestigationDialog(QDialog):
    """Forensic comparison plus explicit Phase-12.4 human disposition controls."""

    resolution_requested = Signal(str, str)
    recovery_planning_requested = Signal(str)

    def __init__(self, investigation, parent=None) -> None:
        super().__init__(parent)
        self._investigation = investigation
        self.setWindowTitle("Hash Mismatch Investigation")
        self.resize(1180, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QLabel("Hash Mismatch Investigation")
        title.setObjectName("SectionHeader")
        root.addWidget(title)

        intro = QLabel(
            "The left side represents the catalogue's expected FileRevision only when "
            "its derivative provenance can be attested. The right side is a fresh "
            "derivative of the bytes currently on disk. Current bytes are observation, "
            "not replacement authority."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        body = QHBoxLayout()
        body.addWidget(self._expected_panel(), 1)
        body.addWidget(self._current_panel(), 1)
        root.addLayout(body, 1)

        if investigation.verify_observed_sha256:
            verified = QLabel(
                f"Most recent Verify mismatch: {_short(investigation.verify_observed_sha256, 32)} "
                f"at {investigation.verify_observed_at or 'unknown time'}"
            )
            verified.setTextInteractionFlags(Qt.TextSelectableByMouse)
            verified.setWordWrap(True)
            root.addWidget(verified)

        if investigation.latest_resolution_action:
            disposition = QLabel(
                "Latest recorded disposition: "
                + investigation.latest_resolution_action.replace("_", " ")
                + f" at {investigation.latest_resolution_at or 'unknown time'}"
                + (f"\nNote: {investigation.latest_resolution_note}" if investigation.latest_resolution_note else "")
            )
            disposition.setWordWrap(True)
            disposition.setStyleSheet(f"color: {theme.AMBER};")
            root.addWidget(disposition)

        if investigation.notes:
            notes = QLabel("\n".join("• " + n for n in investigation.notes))
            notes.setWordWrap(True)
            notes.setStyleSheet(f"color: {theme.TEXT_DIM};")
            root.addWidget(notes)

        decision = QLabel(
            "Resolution records your human decision; it does not edit, restore, move or delete the source file."
        )
        decision.setWordWrap(True)
        decision.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(decision)

        bottom = QHBoxLayout()
        if investigation.latest_resolution_action == "retain_expected_recovery_needed":
            recovery = QPushButton("Plan recovery…")
            recovery.setToolTip(
                "Phase 13.0: qualify exact-copy donors and build a dry-run recovery proposal. "
                "No source photo will be written."
            )
            recovery.clicked.connect(
                lambda: self.recovery_planning_requested.emit(investigation.file_id)
            )
            bottom.addWidget(recovery)

        if investigation.current_state != "matches_expected":
            retain = QPushButton("Keep expected / recovery needed…")
            retain.setToolTip("Retain the catalogue FileRevision as authority and record that recovery is still needed.")
            retain.clicked.connect(lambda: self._request_resolution("retain_expected_recovery_needed"))
            bottom.addWidget(retain)

            unresolved = QPushButton("Record unresolved…")
            unresolved.setToolTip("Record that the mismatch was reviewed but no authority decision was made.")
            unresolved.clicked.connect(lambda: self._request_resolution("reviewed_unresolved"))
            bottom.addWidget(unresolved)

            adopt = QPushButton("Adopt current as new revision…")
            adopt.setEnabled(investigation.current_state == "still_mismatched")
            adopt.setToolTip(
                "Explicitly accept the reviewed current bytes as a new immutable FileRevision. "
                "The source file itself is not changed."
            )
            adopt.clicked.connect(lambda: self._request_resolution("adopt_current_revision"))
            bottom.addWidget(adopt)
        else:
            reconciled = QLabel("Current bytes now match the expected revision. Run Verify to clear the stale health flag.")
            reconciled.setWordWrap(True)
            reconciled.setStyleSheet(f"color: {theme.TEAL};")
            bottom.addWidget(reconciled, 1)

        bottom.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        root.addLayout(bottom)

    def _request_resolution(self, action: str) -> None:
        inv = self._investigation
        if action == "adopt_current_revision":
            text = (
                "Adopt the CURRENT reviewed bytes as a new immutable FileRevision?\n\n"
                f"Expected SHA-256: {inv.expected_sha256}\n"
                f"Current SHA-256:  {inv.current_observed_sha256 or 'unavailable'}\n\n"
                "This asserts that the current bytes are an intentional continuation of this same File/Photo. "
                "PPA will change catalogue authority and mark the File healthy, but it will NOT modify the source file."
            )
            if QMessageBox.warning(
                self, "Adopt current bytes", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            ) != QMessageBox.StandardButton.Yes:
                return
        elif action == "retain_expected_recovery_needed":
            if QMessageBox.question(
                self, "Retain expected revision",
                "Keep the existing expected FileRevision as catalogue authority and record that recovery is still needed?\n\n"
                "The current bytes will not be adopted or changed.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            ) != QMessageBox.StandardButton.Yes:
                return
        else:
            if QMessageBox.question(
                self, "Record unresolved review",
                "Record that you reviewed this mismatch but are leaving it unresolved?\n\nCatalogue authority will not change.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            ) != QMessageBox.StandardButton.Yes:
                return

        note, ok = QInputDialog.getMultiLineText(
            self, "Resolution note", "Optional note (why you made this decision):", ""
        )
        if not ok:
            return
        self.resolution_requested.emit(action, note.strip())
        self.close()

    def _image_label(self, path: str | None, fallback: str) -> QLabel:
        label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        label.setMinimumSize(420, 420)
        label.setStyleSheet(f"background: {theme.OBSIDIAN}; border: 1px solid {theme.BORDER};")
        if path and Path(path).is_file():
            pix = QPixmap(path)
            if not pix.isNull():
                label.setPixmap(pix.scaled(
                    520, 520,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
                return label
        label.setText(fallback)
        label.setWordWrap(True)
        return label

    def _expected_panel(self) -> QWidget:
        inv = self._investigation
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        status = inv.expected_reference_status
        headings = {
            "attested_cache": "EXPECTED / CATALOGUED — ATTESTED CACHE",
            "confirmed_exact_copy": "EXPECTED / CATALOGUED — RECONFIRMED COPY",
            "current_revalidated": "EXPECTED / CATALOGUED — CURRENT BYTES REVALIDATED",
            "legacy_unattested_cache": "CATALOGUE-KEYED LEGACY CACHE — UNATTESTED",
            "unavailable": "EXPECTED / CATALOGUED — REFERENCE UNAVAILABLE",
        }
        heading = QLabel(f"<b>{headings.get(status, status.upper())}</b>")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setWordWrap(True)
        if not inv.expected_reference_attested:
            heading.setStyleSheet(f"color: {theme.AMBER};")
        layout.addWidget(heading)
        layout.addWidget(self._image_label(
            inv.expected_reference_path,
            "No attested expected-image derivative is available.\n"
            "PPA will not regenerate it from mismatching current bytes.",
        ), 1)
        meta = QLabel(
            f"Expected FileRevision SHA-256\n{inv.expected_sha256}\n\n"
            f"Reference status: {status.replace('_', ' ')}"
        )
        meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        meta.setWordWrap(True)
        meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(meta)
        return panel

    def _current_panel(self) -> QWidget:
        inv = self._investigation
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        if inv.current_state == "still_mismatched":
            title = "CURRENT ON-DISK BYTES — UNTRUSTED"
            colour = theme.RED
        elif inv.current_state == "matches_expected":
            title = "CURRENT ON-DISK BYTES — NOW MATCH EXPECTED"
            colour = theme.TEAL
        else:
            title = f"CURRENT ON-DISK BYTES — {inv.current_state.replace('_', ' ').upper()}"
            colour = theme.AMBER
        heading = QLabel(f"<b>{title}</b>")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setStyleSheet(f"color: {colour};")
        layout.addWidget(heading)
        layout.addWidget(self._image_label(
            inv.current_preview_path,
            "Current-byte preview unavailable.",
        ), 1)
        meta = QLabel(
            f"Current observed SHA-256\n{inv.current_observed_sha256 or 'unavailable'}\n\n"
            f"State: {inv.current_state.replace('_', ' ')}"
        )
        meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        meta.setWordWrap(True)
        meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(meta)
        return panel
