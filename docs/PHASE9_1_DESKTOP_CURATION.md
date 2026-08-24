# Phase 9.1 — Album & Tag Desktop Curation

Phase 9.1 turns the Phase-9.0 organisation model into a desktop workflow without changing its authority boundary.

## User workflow

- The main photo grid supports extended selection (Ctrl/Shift).
- **Albums & Tags…** opens a browser/curation dialog for the active Library.
- With selected photos, Album and Tag membership changes operate on logical `Photo` IDs, not physical `File` copies.
- The inspector shows current Album and Tag membership for the selected logical Photo.
- The dialog can also be opened with no selected photos to browse/create Albums and Tags.

## Atomic bulk operations

Bulk Album and Tag operations validate every selected Photo before opening the write transaction. Membership changes and their audit rows are then committed together. If validation or a database guard fails, the entire operation rolls back.

Duplicate selected physical copies of the same logical Photo collapse to one membership operation.

## Authority boundary

Albums and Tags are human organisation only. Phase 9.1 does not write or infer:

- metadata observations;
- anchors;
- reconstructions;
- chronology/Timeline placement;
- Events or Story Context;
- EXIF/source-photo bytes.

A date-looking Album or Tag remains a label, never date evidence.
