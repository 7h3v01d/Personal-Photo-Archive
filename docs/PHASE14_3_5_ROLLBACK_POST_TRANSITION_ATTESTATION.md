# Phase 14.3.5 — Rollback Post-Transition Attestation

Phase 14.3.5 closes the native-Windows rollback-finalization gap found in
adversarial review of 14.3.4.

## Problem

The existing-target rollback path already required two agreeing pre-restore
proofs of the parked suspect: the original still-open handle and a fresh handle
opened through the parked pathname.  It then performed an exact-handle reverse
rename back to the registered target name.  The final success check proved only
filesystem identity.  An already-open writer could therefore modify the same
inode after both pre-restore hashes but before the reverse rename.  Filesystem
identity would remain stable and the helper could incorrectly certify
`aborted_exact_target_restored` for changed bytes.

## Correction

Rollback is now a transition-aware two-sided attestation:

1. prove the original parked handle has the reviewed SHA-256, size, mtime,
   filesystem identity, and `nlink == 1`;
2. independently reopen the parked pathname through the pinned parent and prove
   the same exact snapshot;
3. prove the registered target name is still absent;
4. reverse-rename the original exact handle to the target with no replacement;
5. **after the reverse transition**, hash the original still-open handle again
   against the reviewed suspect snapshot;
6. independently reopen the restored target through the pinned parent and prove
   the same exact bytes, identity, size, mtime, and single-link topology;
7. only then may Phase 14.3 record `aborted_exact_target_restored`.

## Failure rule

Before the reverse rename, uncertainty may return "not restored" and the caller
can retain the parked suspect.  After the reverse rename succeeds, any failed
post-restore proof is transition-aware uncertainty.  The helper raises
`RecoveryTargetExecutionError`; the durable execution attempt remains unresolved
and **no execution-result row is written**.  PPA performs no second automatic
rename merely to regain liveness.

The read-only execution-status inspector continues to expose the current target
state and SHA for unresolved attempts, including a target whose bytes changed
concurrently after the pre-restore proofs.

## Permanent regressions

- platform-neutral: a post-reverse-rename original-handle proof failure must
  propagate as unresolved rather than returning `False`;
- native Windows/NTFS: an independent read/write/delete-sharing handle is kept
  across parking, both pre-restore proofs complete, the same inode is modified
  immediately before the reverse rename, the rename succeeds, final attestation
  detects the changed bytes, the attempt remains unresolved, result count stays
  zero, and status exposes the changed target SHA.

Schema remains **v41**.  No migration is added.
