# Phase 9.10 — Suggestion Review History & Dismissal

Phase 9.10 makes assisted-organisation review state durable without making it organisation truth.

## Invariants

- A dismissal belongs to one exact suggestion fingerprint.
- The fingerprint covers the complete logical peer set, already-tagged support set, target set, source group and Tag identity.
- If that peer/support state changes, the new recommendation receives a different fingerprint and may surface again.
- Dismissed fingerprints are hidden from the normal suggestion view but remain recoverable in review history.
- Restore removes only the active dismissal; append-only history preserves the dismiss/restore actions.
- Accepted suggestions record acceptance history while the audited Tag membership changes in the same SQLite transaction.
- Review state never creates Album membership, Tag membership, Event membership, chronology evidence, anchors, reconstructions, or source-file writes.

## UI

`Assisted Organisation` now provides `Dismiss…` and `Reviewed…` alongside Review/Apply. Dismiss supports an optional human note. Reviewed history exposes accepted and dismissed fingerprints and allows a dismissed fingerprint to be restored.

## Schema

Schema v22 adds:

- `organization_suggestion_reviews` — current review state keyed by `(library_id, suggestion_id)`.
- `organization_suggestion_review_history` — append-only dismiss/accept/restore audit.
