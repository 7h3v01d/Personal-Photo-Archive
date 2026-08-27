# Phase 11.0 — Application Navigation & Workspace Refactor

## Problem

The mature application had accumulated more than twenty top-level toolbar
actions. On a wide desktop the flat action strip could span multiple monitors,
which made feature discovery harder and gave rare diagnostics the same visual
weight as daily archive work.

## Contract

Phase 11.0 is a navigation-only refactor. Existing QAction objects, handlers,
background workers, authority boundaries, and data semantics remain unchanged.

The main toolbar now exposes five stable workspaces:

- **Library** — Add Library, Libraries, Scan, Verify, Extract Metadata.
- **Timeline** — Timeline, Family History, Date Review, Unresolved Memories.
- **Organisation** — Albums & Tags, Albums, Tags, Discover, Assisted
  Organisation, Organisation Health, Organisation Activity, report export.
- **Identity** — Duplicates & Lineage (including all current Phase-10 tabs).
- **Diagnostics** — Pilot Audit, Pilot Session, Activity Log, Activity Runs,
  diagnostics export.

`Refresh` and thumbnail `Size` remain global because they operate on the main
catalogue surface rather than one specialist workspace.

## Safety

This phase performs no database migration and introduces no new persistence.
It changes navigation placement only. Existing action instances are reused so
busy-state disabling, signal connections, worker execution, and tested dialogs
continue through the same code paths.

## Regression gate

A PySide6 smoke regression verifies:

1. all five workspace buttons exist in deterministic order;
2. every former toolbar feature remains reachable in its expected workspace;
3. feature actions no longer appear directly on the toolbar;
4. Refresh remains directly available.

## Phase 11.1.2 — Portable command labels

The command palette no longer derives its canonical search/display label from `QAction.text()`. Qt may interpret `&` as a mnemonic marker differently across platform styles, which caused literal command names such as `Albums & Tags…` and `Duplicates & Lineage` to lose their ampersand on Windows. Palette commands now carry an application-owned display label while the QAction remains the authority for enabled state and dispatch.
