"""Phase 14.4 — desktop gate for the frozen Phase-14.3.5 execution backend."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from ppa.ui import theme


class RecoveryExecutionDialog(QDialog):
    """Require the exact backend-issued phrase before emitting one execution request."""

    execution_requested = Signal(object, str, str)

    def __init__(self, plan, parent=None) -> None:
        super().__init__(parent)
        self._plan = plan
        self.setWindowTitle("Archive Recovery — Source Target Execution")
        self.resize(900, 610)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        title = QLabel("Phase 14.4 — Desktop Recovery Execution Gate")
        title.setObjectName("SectionHeader")
        root.addWidget(title)

        warning = QLabel(
            "SOURCE-MUTATING OPERATION — the frozen Phase-14.3.5 backend may create or replace "
            "the registered source target after it freshly revalidates the entire recovery evidence chain. "
            "This dialog grants no authority by itself. Execution occurs only if the exact confirmation "
            "phrase below is re-entered and the backend accepts the same previewed execution UUID."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {theme.RED}; font-weight: 600;")
        root.addWidget(warning)

        detail = QLabel(
            f"Execution ID: {plan.execution_id}\n"
            f"Readiness ID: {plan.readiness_id}\n"
            f"Target: {plan.target_path}\n"
            f"Current target state: {plan.target_initial_state}\n"
            f"Mode: {plan.replacement_mode}\n"
            f"Expected SHA-256: {plan.expected_sha256}\n"
            f"Execution fingerprint: {plan.execution_plan_fingerprint}\n\n"
            "Preview authority:\n"
            "  Target replacement authorised: NO\n"
            "  Recovery execution authorised: NO"
        )
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail.setWordWrap(True)
        root.addWidget(detail)

        phrase_label = QLabel("Exact confirmation phrase issued by the frozen backend:")
        root.addWidget(phrase_label)
        phrase = QLineEdit(plan.confirmation_phrase, self)
        phrase.setReadOnly(True)
        phrase.setObjectName("recoveryConfirmationPhrase")
        root.addWidget(phrase)

        root.addWidget(QLabel("Type the phrase exactly to enable one execution attempt:"))
        self._confirmation = QLineEdit(self)
        self._confirmation.setObjectName("recoveryConfirmationInput")
        self._confirmation.setPlaceholderText("Exact confirmation phrase")
        root.addWidget(self._confirmation)

        root.addWidget(QLabel("Optional audit note:"))
        self._note = QLineEdit(self)
        self._note.setObjectName("recoveryExecutionNote")
        self._note.setMaxLength(4000)
        root.addWidget(self._note)

        caution = QLabel(
            "If the attempt becomes ambiguous after a namespace transition, PPA will leave it UNRESOLVED "
            "rather than guess, replay, or perform a second speculative mutation. A successful expected-byte "
            "placement still requires ordinary Verify before catalogue health can return to OK."
        )
        caution.setWordWrap(True)
        caution.setStyleSheet(f"color: {theme.AMBER};")
        root.addWidget(caution)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._execute = QPushButton("Execute one recovery attempt", self)
        self._execute.setObjectName("recoveryExecuteButton")
        self._execute.setEnabled(False)
        self._execute.clicked.connect(self._emit_execution)
        buttons.addWidget(self._execute)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        root.addLayout(buttons)

        self._confirmation.textChanged.connect(self._refresh_gate)

    def _refresh_gate(self, value: str) -> None:
        self._execute.setEnabled(value == self._plan.confirmation_phrase)

    def _emit_execution(self) -> None:
        confirmation = self._confirmation.text()
        if confirmation != self._plan.confirmation_phrase:
            return
        self.execution_requested.emit(self._plan, confirmation, self._note.text().strip())
        self.accept()
