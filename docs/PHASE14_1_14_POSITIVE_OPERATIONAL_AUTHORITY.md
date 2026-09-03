# Phase 14.1.14 — Positive Operational Authority

## Problem

Phase 14.1.13 could reject previously observed source-tree directory objects, but no historical denylist can know a source directory created after the last scan and moved away before PPA observes it. Negative authority therefore reached its natural limit.

## Invariant

Writable operational locations are authorized only when the exact filesystem object is positively PPA-owned for that purpose.

```text
bind exact candidate object
        ↓
verify source-tree/path exclusions
        ↓
identity matches enrolled operational object?
        ↓
YES → write authority
NO  → fail closed
```

Migration 039 adds `operational_directories` and `operational_files`. Thumbnail caches and preservation roots are enrolled only when PPA itself creates the final directory object, or through an explicit trusted enrollment operation. Existing un-enrolled objects are never auto-adopted.

Safe export additionally records PPA-owned output-file identities. Catalogue-backed replacement is allowed only for an existing exact file object already recorded as a PPA export. `enroll_export_root()` is the explicit trust boundary for an existing user-selected export directory.

Migration 038 source-tree history remains defense in depth. No changes are made to `BoundTemporaryFile`, `BoundDirectory`, Windows `NtCreateFile` / `NtSetInformationFile`, donor evidence, or recovery target-write authority.

## Required hostile regressions

- post-scan unknown source child moved onto enrolled thumbnail-cache pathname → reject, source bytes unchanged, no marker/attestation;
- post-scan unknown source child moved onto enrolled export-root pathname → reject, sidecar unchanged;
- post-scan unknown source child moved onto enrolled preservation-root pathname → reject, no stage/checkpoint;
- post-scan unknown source leaf moved onto an existing export destination → reject, bytes unchanged.
