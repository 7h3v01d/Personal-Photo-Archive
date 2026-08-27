# Phase 10.3 — Controlled Identity Resolution

Phase 10.3 introduces the first deliberate operation that may change logical Photo identity. It is intentionally limited to splitting one complete current-SHA-256 cohort from an already-divergent logical Photo into a new logical Photo.

## Invariants

- Human initiated only; no automatic split or merge.
- Selected physical Files must form one complete known current-hash cohort.
- The source Photo must currently contain at least two known current hashes.
- The source Photo must retain at least one physical File after the split.
- A cohort spanning multiple Libraries is refused in Phase 10.3.
- If the selected SHA-256 already belongs to another logical Photo, the split is refused rather than multiplying an identity inconsistency.
- Existing Album/Tag membership may not be stranded by removing the source Photo's last representation from that Library.
- The review plan fingerprints the complete current source-Photo File state.
- Commit uses `BEGIN IMMEDIATE`, rebuilds the plan under the write lock, and refuses stale evidence.
- The operation creates a new Photo row and reassigns File catalogue ownership only. FileRevision rows, paths, bytes, EXIF, chronology, Events, Albums, Tags and lineage are not rewritten or copied.
- An append-only `identity_resolution_history` row records the exact moved cohort and evidence fingerprint.

## UI

`Duplicates & Lineage` → `Identity Divergence` now includes **Split selected hash cohort…**. The user selects File rows from one divergent logical Photo, reviews the proposed cohort, confirms explicitly, and the dialog closes after successful atomic resolution so the identity projection can be reopened fresh.

## CLI

Dry-run review:

```text
python -m ppa.cli identity-split <library-id> <source-photo-id> <file-id> [<file-id> ...]
```

Apply after review:

```text
python -m ppa.cli identity-split <library-id> <source-photo-id> <file-id> [<file-id> ...] --apply --note "Human review note"
```
