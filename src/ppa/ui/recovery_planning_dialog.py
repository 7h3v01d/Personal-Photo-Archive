"""Phase 13.0 recovery donor qualification and dry-run proposal UI."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

from ppa.ui import theme


def _short(value: str | None, n: int = 24) -> str:
    if not value:
        return "unavailable"
    return value if len(value) <= n else value[:n] + "…"


class RecoveryPlanningDialog(QDialog):
    """Show donor qualification and the preferred non-executable recovery plan."""

    proposal_requested = Signal(object, str)

    def __init__(self, view, plan, parent=None) -> None:
        super().__init__(parent)
        self._view = view
        self._plan = plan
        self.setWindowTitle("Archive Recovery Planning — Dry Run")
        self.resize(980, 720)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(9)

        title = QLabel("Phase 13.0 — Recovery Planning & Donor Qualification")
        title.setObjectName("SectionHeader")
        root.addWidget(title)

        warning = QLabel(
            "DRY RUN ONLY — this phase does not restore, replace, rename, move, delete, "
            "or otherwise write a source photograph. It proves donor evidence and records "
            "what a later recovery phase would have to do."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {theme.AMBER};")
        root.addWidget(warning)

        target = QLabel(
            f"Target File: {view.file_id}\n"
            f"Path: {view.path}\n"
            f"Expected SHA-256: {view.expected_sha256}\n"
            f"Current target state: {view.target_state.replace('_', ' ')}\n"
            f"Recovery intent: {view.recovery_intent_resolution_id}"
        )
        target.setTextInteractionFlags(Qt.TextSelectableByMouse)
        target.setWordWrap(True)
        root.addWidget(target)

        root.addWidget(QLabel("Donor qualification"))
        candidates = QListWidget(self)
        for candidate in view.candidates:
            label = "QUALIFIED" if candidate.qualified else "REJECTED"
            detail = candidate.topology_class.replace("_", " ")
            if candidate.rejection_reasons:
                detail += " — " + "; ".join(candidate.rejection_reasons)
            item = QListWidgetItem(
                f"{label} · Library {candidate.library_id} · {candidate.path}\n"
                f"SHA {_short(candidate.physical_sha256 or candidate.expected_sha256)} · {detail}"
            )
            if not candidate.qualified:
                item.setForeground(Qt.GlobalColor.gray)
            candidates.addItem(item)
        if candidates.count() == 0:
            candidates.addItem("No catalogue Files share the immutable expected revision SHA-256.")
        root.addWidget(candidates, 1)

        if plan is not None:
            plan_text = QLabel(
                f"Preferred donor: {plan.donor_file_id} · Library {plan.donor_library_id}\n{plan.donor_path}\n"
                f"Topology: {plan.topology_class.replace('_', ' ')}\n"
                "Independent backup proven: NO\n"
                f"Evidence fingerprint: {plan.evidence_fingerprint}\n\n"
                "Proposed future action:\n" +
                "\n".join(f"  {i}. {step}" for i, step in enumerate(plan.proposed_action, start=1))
            )
            plan_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            plan_text.setWordWrap(True)
            root.addWidget(plan_text)
        else:
            no_plan = QLabel(
                "No donor currently qualifies. Recovery cannot be planned until a donor "
                "reproduces the expected SHA-256 under both catalogue and fresh physical evidence."
            )
            no_plan.setWordWrap(True)
            no_plan.setStyleSheet(f"color: {theme.RED};")
            root.addWidget(no_plan)

        if view.notes:
            notes = QLabel("\n".join("• " + note for note in view.notes))
            notes.setWordWrap(True)
            notes.setStyleSheet(f"color: {theme.TEXT_DIM};")
            root.addWidget(notes)

        buttons = QHBoxLayout()
        if plan is not None:
            record = QPushButton("Record preferred dry-run proposal…")
            record.setToolTip(
                "Append this proposal and evidence fingerprint to the catalogue audit ledger. "
                "No recovery write will occur."
            )
            record.clicked.connect(self._record)
            buttons.addWidget(record)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        root.addLayout(buttons)

    def _record(self) -> None:
        if self._plan is None:
            return
        if QMessageBox.question(
            self,
            "Record dry-run recovery proposal",
            "Record this recovery proposal and its evidence fingerprint in the catalogue?\n\n"
            "This DOES NOT authorise or perform recovery and DOES NOT write the source photograph.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        note, ok = QInputDialog.getMultiLineText(
            self, "Recovery proposal note", "Optional planning note:", ""
        )
        if not ok:
            return
        self.proposal_requested.emit(self._plan, note.strip())
        self.close()
