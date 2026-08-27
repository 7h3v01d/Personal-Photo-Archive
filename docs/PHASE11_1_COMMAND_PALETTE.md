# Phase 11.1 — Command Palette & Keyboard Navigation

Phase 11.1 adds keyboard-first navigation without creating any new command authority.

## Global command palette

- `Ctrl+Shift+P` opens **Command Palette**.
- The compact toolbar also exposes **Commands…** for discoverability.
- Search is case-insensitive AND-token matching across workspace + command name.
- Enter, Run, or double-click dispatches the existing canonical `QAction`.
- Disabled commands remain visible as **unavailable** and cannot bypass busy-state protection.

## Workspace shortcuts

- `Alt+1` — Library
- `Alt+2` — Timeline
- `Alt+3` — Organisation
- `Alt+4` — Identity
- `Alt+5` — Diagnostics

The shortcuts open the exact same workspace menus used by mouse navigation.

## Architectural rule

The palette and workspace shortcuts are navigation surfaces only. Existing `QAction` objects continue to own command handlers, enabled/disabled state, and operational safety rules.

No database schema change and no archive/source-photo mutation is introduced by this phase.
