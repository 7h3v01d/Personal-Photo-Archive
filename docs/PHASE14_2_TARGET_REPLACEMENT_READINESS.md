# Phase 14.2 — Target-Replacement Readiness Protocol

Phase 14.1.17.4.1 is frozen. It completed the recovery-staging foundation: suspect target bytes can be preserved in protected operational storage, expected donor bytes can be separately materialized and re-attested, and the recovery evidence chain is hardened against namespace substitution, source-object adoption, destructive-cleanup races, late hard links, and Windows CRT binary-descriptor semantics.

Phase 14.2 deliberately **does not execute recovery**. Its job is narrower:

> Given one committed Phase-14.0 preservation checkpoint and one committed Phase-14.1 donor-materialization checkpoint, is the exact recovery chain still physically and logically coherent enough to be reviewed for a future target-replacement protocol?

A positive Phase-14.2 answer is evidence, not authority.

## Authority boundary

Every Phase-14.2 object carries:

```text
target_replacement_authorized = false
recovery_execution_authorized = false
```

The implementation contains no target-create, target-replace, rename, delete, chmod, metadata-write, timestamp-repair, or EXIF-write path.

The only persistent write Phase 14.2 can perform is an explicit append-only SQLite readiness record plus its audit event.

## Required evidence chain

Readiness starts from one committed `archive_recovery_donor_materializations` row and requires the exact chain:

```text
Phase-13 dry-run proposal
        ↓
Phase-14.0 preservation checkpoint
        ↓
Phase-14.1 verified donor materialization
        ↓
Phase-14.2 fresh readiness observation
```

The proposal/stage/materialization IDs, target File, expected FileRevision, expected SHA-256, human recovery-intent resolution, and immutable evidence fingerprints must all agree.

## Human and catalogue authority

The target File must still:

- be registered in the same Library;
- retain the same recorded target pathname;
- retain the same immutable expected FileRevision;
- remain `health_status='hash_mismatch'`;
- have the same latest human disposition `retain_expected_recovery_needed` that authorised the frozen recovery chain.

If any later mismatch decision supersedes that intent, readiness fails closed.

## Current destination observation

The current target is freshly observed with the stable Phase-12.4.2 physical-observation primitive.

It must **not** already reproduce the expected SHA-256. If it does, the correct next action is Verify, not recovery.

The observed target must still exactly reproduce the physical state captured by the committed preservation checkpoint.

Two future destination modes are distinguished without authorising either:

```text
still-mismatched / unreadable target
→ replace_existing_exact_target

still-missing target
→ restore_missing_recorded_target
```

For a present target, current hard-link count must be exactly 1. A multi-linked source object is not silently interpreted; Phase 14.2 refuses automatic readiness and requires explicit manual/topology review.

## Registered-root + destination-parent source-tree proof

Before readiness can be reported, the registered Library root itself must still be:

- a real directory;
- non-reparse / non-symlink;
- the exact filesystem object recorded in `libraries.root_fs_device_id/root_fs_object_id`;
- part of a completely verified source-tree authority snapshot.

Only after that root proof succeeds may the current parent directory of the recorded target path count as valid. The parent must be:

- a real directory;
- non-reparse / non-symlink;
- inside the verified registered Library root;
- an exact filesystem directory object already present in that Library's complete `library_directory_identities` inventory.

This is **not write authority**. It records the exact source-tree topology a future execution phase would have to bind and independently revalidate before any namespace mutation could be considered.

A fake replacement Library root therefore cannot become ready merely by receiving a transplanted historically-known child directory.

## Phase 14.2.1 destination final attestation

Adversarial review of 14.2 found that the destination was observed before the potentially lengthy preservation/donor evidence hashing and was not re-established afterward. That could allow a readiness object, or even an immutable recorded readiness row, to describe a target or target-parent object that had already changed before the readiness operation completed.

14.2.1 therefore treats destination freshness as a two-snapshot contract:

```text
initial source-parent authority + target snapshot
        ↓
all Phase-14 operational evidence hashing
        ↓
FINAL source-parent authority + target snapshot
        ↓
require exact equality
        ↓
construct readiness fingerprint
```

For a present target, the accepted target snapshot binds the link-count metadata observation to the exact stable content observation: device/object identity, size and mtime must all agree before `st_nlink == 1` is accepted. The final snapshot must match the initial target state, SHA-256, size, mtime, filesystem identity and link count exactly. For a missing target, absence must still be re-established.

The final source-parent check re-runs the verified Library directory-identity proof and requires the exact same canonical parent path and filesystem object identity observed initially.

This remains read-only. It closes a meaningful evidence-hashing window but does not pretend to create an atomic transaction spanning SQLite and the filesystem. A later execution phase must independently re-attest again.


## Phase 14.2.2 Library-root topology attestation

Adversarial review of 14.2.1 showed that a genuine, historically verified immediate target-parent directory could be transplanted beneath a newly created replacement Library root. Because 14.2.1 verified the child identity but not the current root object against the persisted Library-root identity, the resulting false topology could still produce and record readiness.

14.2.2 extends both destination attestations to prove three nested objects:

```text
registered Library root object
        ↓
historically verified immediate target-parent object
        ↓
identity-bound target snapshot
```

The current object at `root_canonical_path` must exactly match `libraries.root_fs_device_id/root_fs_object_id`. That proof runs before the initial target snapshot and again after all Phase-14 operational-evidence hashing. The final root path/identity, parent path/identity and target snapshot must match the initial accepted topology before readiness fingerprint construction.

The exact registered root canonical path and filesystem identity are included in the readiness evidence fingerprint. This is still read-only planning evidence and does not grant namespace mutation authority.

## Phase 14.2.3 bound destination topology finalization

Adversarial review of 14.2.2 showed that a root or target-parent pathname could still be replaced *inside* the final attestation after its last pathname `lstat()` but before the potentially lengthy final target hash completed. A genuine known parent or exact target object could be transplanted into the replacement namespace, allowing the target observation to succeed while the earlier directory-topology observation had already become stale.

14.2.3 therefore reuses the hardened Phase-14.1 directory-authority primitive as a read-only identity pin during finalization:

```text
re-establish registered root + known parent policy
        ↓
bind exact registered Library root object
        ↓
bind exact target-parent object
        ↓
keep both pins open
        ↓
final stable target observation/hash
        ↓
verify root pathname still names bound root
        ↓
verify parent pathname still names bound parent
        ↓
compare target snapshot
        ↓
readiness fingerprint
```

The bound authority identities must exactly equal the already accepted initial/persisted root and parent identities. Any bind or `verify_pathname()` failure becomes a readiness rejection requiring rescan/review. The authority objects are never used for child mutation in Phase 14.2; they provide observation/freshness guarantees only.

## Operational evidence re-attestation

Phase 14.2 re-proves the current operational evidence without mutating it:

- preserved suspect copy when one exists;
- Phase-14.0 preservation manifest;
- materialized expected donor;
- filesystem donor manifest, or the canonical embedded-manifest payload.

Filesystem evidence is descriptor-bound hashed and must remain:

- regular;
- non-reparse;
- exact current filesystem object across open/hash;
- single-link before and after hashing;
- exact expected size where recorded;
- exact expected SHA-256.

Historical source-object exclusion is intentionally not re-applied to already committed operational evidence at this later read-only boundary. Phase 14 enforced source-object exclusion at creation/adoption time. Repeating only `(device,inode)` historical exclusion later would falsely reject legitimate operational files after POSIX inode-number reuse. Phase 14.2 therefore treats the immutable Phase-14 checkpoint plus current exact content/link attestation as the relevant evidence boundary.

## Immutable readiness record

Migration 040 adds `archive_recovery_target_readiness`.

A recorded row contains:

- readiness/materialization/stage/proposal IDs;
- target File/Library/expected revision/SHA;
- exact current target observation;
- target link count;
- exact current target-parent filesystem identity;
- replacement-mode classification;
- preservation evidence paths/hashes;
- materialized donor path/hash/size;
- donor-manifest representation/hash;
- immutable evidence fingerprint;
- explicit zero-valued target-replacement/execution authority flags.

Rows cannot be updated or deleted.

Recording re-runs the complete readiness calculation under `BEGIN IMMEDIATE`. If the physical or catalogue evidence changes between review and recording, no readiness row is appended.

## CLI

```text
python -m ppa.cli recovery-target-readiness <materialization-id>
python -m ppa.cli recovery-target-readiness <materialization-id> --record
python -m ppa.cli recovery-target-readiness <materialization-id> --record --note "reviewed only"
python -m ppa.cli recovery-target-readiness <materialization-id> --json readiness.json
```

`--record` writes only the immutable catalogue readiness snapshot. It does not execute recovery.

## Permanent regressions

Phase 14.2 includes regressions proving:

1. ordinary readiness is read-only and grants zero target authority;
2. explicit recording is append-only and still grants zero target authority;
3. a missing target is classified as restore readiness without creating the target;
4. a target changed after donor materialization invalidates readiness;
5. materialized donor tamper invalidates readiness;
6. preservation-evidence tamper invalidates readiness;
7. a late target hard link blocks readiness without metadata mutation;
8. replacement of the recorded Library/target-parent directory object blocks readiness;
9. recording revalidates and rejects a stale previously displayed snapshot;
10. schema v40 enforces zero-only execution-authority columns;
11. a target changed during operational-evidence hashing is rejected by final attestation;
12. a target-parent directory swapped during operational-evidence hashing is rejected;
13. a record-time rebuild race commits neither a stale readiness row nor a readiness event;
14. the `lstat()` supplying target link count must describe the same exact object as the stable target observation;
15. a substituted registered Library root with a transplanted genuine historical child directory is rejected for both build and record paths;
16. a Library root swapped during Phase-14 evidence hashing while retaining the genuine child directory is rejected by final root attestation;
17. a registered Library root swapped during the final target observation, after both final directory objects are bound, is rejected by the post-hash root pathname verification;
18. a target parent swapped during the final target observation while the exact target object is transplanted is rejected by the post-hash parent pathname verification;
19. the root-swap interleaving during record-time rebuild commits no readiness row and no readiness-recorded event;
20. the parent-swap interleaving during record-time rebuild commits no readiness row and no readiness-recorded event.

## Phase 14.3 boundary

Phase 14.3 now defines the first **actual target-replacement execution semantics** from one freshly revalidated, immutably recorded Phase-14.2 readiness chain. It does not simply "execute" this row: preview remains zero-authority, explicit one-attempt confirmation is recorded before mutation, and all source-write semantics are separately re-attested. See `PHASE14_3_TARGET_REPLACEMENT_EXECUTION.md`.

Phase 14.3 separately specifies and adversarially tests at least:

- exact source-parent write authority;
- present-target replacement versus missing-target creation;
- destination namespace races;
- atomic no-replace/replace semantics appropriate to each mode;
- preservation of the displaced suspect object;
- rollback and crash/interruption behavior;
- Windows/NTFS handle-relative source-tree namespace mutation;
- network/non-local filesystem refusal or semantics;
- post-placement descriptor-bound hash verification;
- Verify-owned reconciliation back to healthy catalogue state.

Phase 14.2 itself remains **planning-only and frozen**. Phase 14.3 is the separate execution boundary and remains active pending adversarial acceptance.
