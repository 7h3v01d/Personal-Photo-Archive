# Phase 14.2.1 — Destination Final Attestation

## Purpose

Phase 14.2 is planning/audit only. Adversarial review proved that its initial target and source-parent observations could become stale while later Phase-14 preservation/donor evidence was being hashed. Because the readiness fingerprint was constructed without one final destination observation, an immutable readiness row could describe a destination state that was already false at commit time.

14.2.1 closes that specific finalisation gap without changing any source-write or recovery-execution authority.

## Contract

Readiness now requires:

1. initial verified target-parent source-tree object;
2. initial identity-bound target snapshot;
3. complete Phase-14 operational-evidence re-attestation;
4. final verified target-parent source-tree object;
5. final identity-bound target snapshot;
6. exact equality between initial and final destination snapshots;
7. only then readiness fingerprint construction / optional SQLite recording.

For present targets the snapshot binds:

- stable target state and SHA-256;
- size and mtime;
- filesystem device/object identity;
- regular/non-reparse status;
- `st_nlink == 1`.

The metadata observation supplying `st_nlink` must itself match the stable observation's exact identity, size and mtime.

For missing targets, absence is re-established at both destination observations.

The final parent check must resolve to the same canonical parent pathname and the same verified Library directory filesystem identity as the initial check.

## Recording

`record_target_replacement_readiness()` already rebuilds under `BEGIN IMMEDIATE`. The rebuild now includes the final destination attestation, so a target or parent changed during record-time evidence hashing causes rollback: no readiness row and no readiness-recorded event.

SQLite does not lock the filesystem, so 14.2.1 does not claim impossible cross-filesystem/database atomicity. The important fix is removal of the large operational-evidence-hashing interval after the last destination observation. Phase 14.3 must independently re-attest before any future mutation.

## Authority

Unchanged:

```text
target_replacement_authorized = false
recovery_execution_authorized = false
```

No target create, replace, rename, delete, chmod, metadata repair, timestamp repair or EXIF write is introduced. Schema remains v40 / migration count 40.

## Permanent regressions

14.2.1 adds four adversarial regressions:

1. target content/object changes after initial destination checks but during evidence hashing → reject;
2. target parent directory object changes during evidence hashing → reject;
3. the same destination race during record-time rebuild → no row and no audit event;
4. target replacement between stable content observation and `nlink` metadata observation → reject because identities differ.
