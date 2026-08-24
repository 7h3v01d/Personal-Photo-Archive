# Phase 9.3 — Album Curation & Presentation

Phase 9.3 adds display-only human presentation preferences to durable Albums.

## Invariants

- Album membership remains logical-Photo based.
- Preferred cover is a current Album `photo_id`, never a physical File identity.
- Custom order is valid only when it is an exact permutation of current Album membership.
- Adding/removing members invalidates custom order. Removing the chosen cover clears only the cover preference.
- Presentation actions are append-audited in `album_presentation_history`.
- Presentation never changes chronology, metadata observations, anchors, reconstructions, Events, EXIF, or source bytes.

## Schema v20

Adds `album_presentation` and `album_presentation_history`, plus DB triggers enforcing current membership for covers and invalidating stale presentation dependencies after membership changes.

## Desktop

`Albums & Tags…` now exposes `Album presentation…`, with Set cover, Move up/down, Save order, and Reset controls.
Album browsing honours valid human order; otherwise it retains deterministic filename ordering.
