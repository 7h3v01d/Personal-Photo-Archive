# Phase 9.11 — Organisation Activity & Change History

Phase 9.11 exposes the existing append-only `organization_history` ledger as a human-readable recent activity view.

## Safe automatic undo

Automatic undo is intentionally limited to direct Album/Tag membership actions (`add_photo`, `remove_photo`). A history row is undoable only when:

- it belongs to the requested Library;
- the Album/Tag still exists;
- it is the latest state-changing membership action for that exact object + logical Photo pair;
- the current membership state still exactly matches the action being reversed;
- when restoring a removed member, the logical Photo is still represented in that Library.

Undo creates a new append-only audit row (`undo_add_photo` or `undo_remove_photo`). It never deletes or rewrites history.

Renames and descriptions are visible but deliberately not one-click reversible in this slice because later naming collisions or edits can make historical text reversal ambiguous.

## Authority boundary

Organisation activity and undo do not read or write chronology evidence, metadata observations, anchors, reconstructions, Event state, EXIF or source-photo bytes. Album presentation membership triggers continue to invalidate stale custom order/cover state exactly as normal membership edits do.
