# Phase 9.7 — Saved Organisation Views

Saved Organisation Views are durable **query recipes**, not result snapshots.
They store a human name plus explicit Album IDs and Tag IDs. Matching logical
Photo IDs are always recomputed from current membership when the view is run.

## Invariants

- At least one Album or Tag selector is required.
- Every selector must exist and belong to the saved view's Library.
- Names are unique per Library using case-insensitive comparison.
- Duplicate selector IDs are collapsed while preserving selector order.
- No Photo IDs are persisted in the saved-view table.
- Evaluation reuses Phase 9.6 exact intersection semantics.
- Saved views never write chronology, evidence, Event, EXIF, or source-photo state.

Schema v21 adds `saved_organization_views`.
