# Phase 7.4 — Pilot Session Dashboard & Guided Workflow

Phase 7.4 adds a desktop orchestration surface over the Phase 7.3 external pilot-session artifact.
It adds no chronology inference, no database schema, and no source-photo write path.

## Workflow

1. Open **Pilot Session…** from the main toolbar.
2. Start a new pilot or load an existing integrity-checked `ppa-pilot-session/1` JSON artifact.
3. For a loaded open session, PPA refreshes the current audit against the saved scope before enabling review actions.
4. Continue **Date Review** or **Unresolved Memories** inside that exact scope.
5. Capture named checkpoints as useful milestones.
6. Close the pilot to record a final snapshot and baseline-to-final comparison.

## Scope safety

A loaded open session is not considered actionable until a current audit successfully resolves to the original:

- library root;
- directory prefix; and
- explicit file-ID selection, when present.

This prevents a reused integer library ID from steering review into an unrelated library.

## Dashboard metrics

The dashboard presents baseline-to-current deltas for:

- usable chronology;
- confirmed current reconstructions;
- unresolved chronology;
- stale decisions;
- actionable review items; and
- anchor questions.

Guidance is deterministic: stale decisions first, then actionable Date Review work, then unresolved browsing, then pilot closure when no unresolved chronology remains.

## Threading

Start, refresh, checkpoint and close operations run through `PilotSessionWorker`, which owns a separate SQLite connection. The GUI event loop is never used for collection-wide audit work. Session files are saved through the atomic writer from Phase 7.3.

## Authority boundary

The dashboard does not confirm, reject, reconstruct, create anchors, edit metadata, or modify source photographs. It only orchestrates existing hardened workflows and external pilot-session measurements.
