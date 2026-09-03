# Phase 14.1.16 — Atomic Namespace Install / Non-Destructive Rollback

## Purpose

Phase 14.1.16 hardens the shared `ppa.secure_write` namespace mutation layer.
Phase 14.1.15 correctly bound ownership decisions to filesystem identities, but
an adversarial review demonstrated that the later namespace operation could
still destroy a different object that arrived after the identity check.

No database or ownership-model change is required. Migration 039 remains the
current schema boundary (`schema_version = 39`).

## Security invariant

> No namespace mutation may replace or delete an object whose exact identity has
> not itself been authorised.

A prior `exists()`/identity check is not authority for whatever later occupies
that pathname. The no-replace condition must be enforced by the filesystem
operation itself, and rollback must prefer recoverable debris over destructive
cleanup.

## 1. POSIX atomic no-replace installation

`BoundDirectory.rename_child_noreplace()` is the POSIX installation primitive.
On Linux it invokes `renameat2(..., RENAME_NOREPLACE)` relative to the already
bound parent directory descriptor.

There is deliberately no fallback to ordinary `rename()` when atomic no-replace
semantics are unavailable. Unsupported platforms/filesystems fail closed.

Consequences:

- a destination that appears immediately before final installation produces
  `EEXIST`;
- the late-arriving object is not replaced;
- the secured temporary object remains PPA operational debris rather than being
  deleted through a raced pathname;
- final installation cannot report success after overwriting a source object.

## 2. No-replace backup parking

Parking an existing authorised destination also uses the no-replace primitive.
The backup name is random and exclusively acquired by the filesystem operation.

After parking, the parked object's exact identity is checked against the
identity authorised for replacement. If a substitution won the source-name race,
the unexpected object is preserved as rollback debris and installation fails.
It is never deleted in an attempt to repair the pathname.

## 3. Non-destructive rollback

Rollback no longer performs:

```text
if destination exists:
    unlink(destination)
restore backup with replacement
```

Instead:

- if the destination is occupied, rollback does not touch it;
- the previous PPA-owned object remains parked as recovery debris;
- restoration is attempted only into an empty slot with atomic no-replace
  semantics;
- if the slot becomes occupied during restoration, both objects remain intact
  and the operation reports failure/manual-recovery state.

Operational debris is explicitly preferable to source-data loss.

## 4. Windows rollback correction

Windows already had native handle-relative final installation with
`replace=False`.

Phase 14.1.16 changes rollback so that:

- an installed PPA object may be deleted only through its exact bound native
  handle;
- a parked backup is restored only with `replace=False`;
- if an unexpected object occupies the destination, it is left untouched and
  the previous PPA object remains parked;
- successful backup cleanup remains handle-relative to the exact parked object.

The permanent post-parking regression contains a native Windows branch and must
run on the normal Windows/NTFS release gate.

## 5. POSIX cleanup authority

POSIX does not provide a general portable inode-bound compare-and-unlink
primitive.

Therefore Phase 14.1.16 removes pathname cleanup that would perform an identity
check followed by `unlink(name)` for secured temporary files. Failed/aborted
PPA temporaries may remain as operational debris. Likewise, previous PPA-owned
backup files may remain after successful replacement rather than being removed
through a check/use deletion window.

This is a deliberate safety/liveness tradeoff, not an accidental leak of source
write authority.

## Permanent adversarial regressions

Phase 14.1.16 adds three attack regressions:

1. **New-destination race** — destination absent, source object inserted
   immediately before final install. Required result: failure; source bytes
   survive at the destination; PPA does not replace them.
2. **Post-parking rollback race** — an owned destination is parked, then a source
   object is inserted into the vacated destination before installation.
   Required result: failure; source survives byte-for-byte; previous PPA output
   remains recoverable as rollback debris.
3. **Thumbnail equivalent** — the same post-parking race against a positively
   owned thumbnail child using the shared installer. Required result: generation
   fails; source child survives; previous thumbnail remains recoverable.

The safe-export post-parking test contains both POSIX and native Windows attack
branches.

## Regression result

Final development gate for this build:

- complete repository: **667 passed, 6 skipped, 0 failed**;
- high-risk safe-export / thumbnail / preservation / donor / hardening set:
  **112 passed, 2 skipped, 0 failed**;
- schema remains **39**.

## Release note

Phase 14.1.16 should be adversarially reviewed before freeze. Reviewers should
attack the exact final no-replace syscall boundary, the post-parking rollback
window, Windows rollback restoration, retained-backup behavior, and any remaining
path-based destructive cleanup in shared filesystem primitives.
