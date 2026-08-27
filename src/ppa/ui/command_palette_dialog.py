"""Keyboard-first command palette for the desktop application.

The palette is a navigation surface only. It never owns command handlers:
canonical ``QAction`` instances remain the single source of enabled state,
shortcuts, and dispatch behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_COMMAND_ROLE = Qt.ItemDataRole.UserRole


@dataclass(frozen=True)
class PaletteCommand:
    workspace: str
    action: QAction
    display_label: str
    description: str = ""

    @property
    def label(self) -> str:
        # Never derive the palette's human label from QAction.text(). Qt may
        # interpret ampersands as mnemonic markers differently across platform
        # styles. The application-owned display label is the canonical command
        # name for search, tests, and non-menu presentation.
        return self.display_label

    @property
    def search_text(self) -> str:
        return f"{self.workspace} {self.label} {self.description}".casefold()


class CommandPaletteDialog(QDialog):
    """Search and dispatch canonical application actions."""

    def __init__(
        self,
        commands: list[PaletteCommand],
        parent: QWidget | None = None,
        *,
        recent_labels: list[str] | None = None,
        on_command_run=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Command Palette")
        self.setObjectName("CommandPaletteDialog")
        self.resize(620, 430)
        self.setModal(True)
        self._commands = list(commands)
        self._recent_labels = list(recent_labels or [])[:5]
        self._on_command_run = on_command_run
        for command in self._commands:
            command.action.changed.connect(self._refresh_for_action_change)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        title = QLabel("Command Palette")
        title.setObjectName("InspectorTitle")
        root.addWidget(title)

        hint = QLabel(
            "Type to filter by workspace, command, or description. Enter runs the selected command; ↑/↓ moves selection. "
            "Recent commands appear first when search is empty. Disabled commands remain visible but cannot be bypassed."
        )
        hint.setWordWrap(True)
        hint.setObjectName("FieldKey")
        root.addWidget(hint)

        self.search = QLineEdit()
        self.search.setObjectName("CommandPaletteSearch")
        self.search.setPlaceholderText("Search commands…")
        self.search.textChanged.connect(self._rebuild)
        self.search.returnPressed.connect(self._run_current)
        root.addWidget(self.search)

        self.list = QListWidget()
        self.list.setObjectName("CommandPaletteList")
        self.list.itemDoubleClicked.connect(lambda _item: self._run_current())
        self.list.currentItemChanged.connect(lambda *_args: self._sync_selection())
        root.addWidget(self.list, 1)

        self.detail = QLabel()
        self.detail.setObjectName("CommandPaletteDetail")
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(34)
        root.addWidget(self.detail)

        controls = QHBoxLayout()
        controls.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.run_button = QPushButton("Run")
        self.run_button.setObjectName("CommandPaletteRunButton")
        self.run_button.clicked.connect(self._run_current)
        controls.addWidget(cancel)
        controls.addWidget(self.run_button)
        root.addLayout(controls)

        self._rebuild("")
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _refresh_for_action_change(self) -> None:
        self._rebuild(self.search.text())

    def _matches(self, command: PaletteCommand, query: str) -> bool:
        tokens = [token for token in query.casefold().split() if token]
        return all(token in command.search_text for token in tokens)

    def _match_rank(self, command: PaletteCommand, query: str) -> tuple[int, int]:
        """Rank matches without hiding useful description-based results.

        An exact command-name query is the strongest signal, followed by
        command-name token matches, workspace+command matches, and finally
        matches that require descriptive metadata.  The second tuple member is
        supplied by ``_ordered_indices`` to preserve deterministic registry
        order within a tier.
        """
        normalized = " ".join(query.casefold().split())
        label = " ".join(command.label.casefold().split())
        workspace = " ".join(command.workspace.casefold().split())
        tokens = normalized.split()

        if normalized == label:
            return (0, 0)
        if tokens and all(token in label for token in tokens):
            return (1, 0)
        workspace_label = f"{workspace} {label}"
        if tokens and all(token in workspace_label for token in tokens):
            return (2, 0)
        return (3, 0)

    def _ordered_indices(self, query: str) -> list[int]:
        indices = [i for i, command in enumerate(self._commands) if self._matches(command, query)]
        if query.strip():
            return sorted(indices, key=lambda i: (self._match_rank(self._commands[i], query)[0], i))
        recent_rank = {label: rank for rank, label in enumerate(self._recent_labels)}
        return sorted(
            indices,
            key=lambda i: (
                0 if self._commands[i].label in recent_rank else 1,
                recent_rank.get(self._commands[i].label, i),
                i,
            ),
        )

    def _rebuild(self, query: str) -> None:
        previous = self.current_command()
        self.list.clear()
        for index in self._ordered_indices(query):
            command = self._commands[index]
            unavailable = "  — unavailable" if not command.action.isEnabled() else ""
            recent = "  · recent" if not query.strip() and command.label in self._recent_labels else ""
            shortcut = command.action.shortcut().toString()
            shortcut_text = f"  [{shortcut}]" if shortcut else ""
            subtitle = command.workspace
            if command.description:
                subtitle += f" · {command.description}"
            item = QListWidgetItem(f"{command.label}{shortcut_text}{recent}{unavailable}\n{subtitle}")
            item.setData(_COMMAND_ROLE, index)
            item.setToolTip(command.description or command.action.toolTip() or command.label)
            if not command.action.isEnabled():
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(item)
            if previous is command:
                self.list.setCurrentItem(item)

        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self._sync_selection()

    def current_command(self) -> PaletteCommand | None:
        item = self.list.currentItem()
        if item is None:
            return None
        index = item.data(_COMMAND_ROLE)
        if not isinstance(index, int) or not (0 <= index < len(self._commands)):
            return None
        return self._commands[index]

    def _sync_selection(self) -> None:
        command = self.current_command()
        self.run_button.setEnabled(bool(command and command.action.isEnabled()))
        if command is None:
            self.detail.setText("No matching command.")
            return
        state = "Available" if command.action.isEnabled() else "Unavailable while another archive operation is active"
        description = command.description or command.action.toolTip() or command.label
        self.detail.setText(f"{command.workspace} · {state} — {description}")

    def _sync_run_button(self) -> None:
        # Backward-compatible helper retained for existing tests/callers.
        self._sync_selection()

    def _run_current(self) -> None:
        command = self.current_command()
        if command is None or not command.action.isEnabled():
            return
        self.accept()
        command.action.trigger()
        if self._on_command_run is not None:
            self._on_command_run(command)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.search.hasFocus():
            if self.list.count():
                row = self.list.currentRow()
                delta = 1 if event.key() == Qt.Key.Key_Down else -1
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row + delta)))
            event.accept()
            return
        super().keyPressEvent(event)
