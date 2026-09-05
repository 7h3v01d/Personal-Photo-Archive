# Phase 14.2.2 — Library Root Topology Attestation

## Purpose

Phase 14.2.1 correctly re-attested the target and its immediate parent after operational-evidence hashing, but adversarial review demonstrated that an already-known child directory could be transplanted beneath a new directory object occupying the registered Library root pathname. The leaf parent remained genuine while the Library root itself no longer matched the persisted registered root object.

14.2.2 closes that topology gap without adding source-write or recovery-execution authority.

## Contract

Every initial and final destination topology attestation now proves:

1. the Library record has a persisted root filesystem identity and complete source-tree authority;
2. `root_canonical_path` currently names a real, non-symlink, non-reparse directory;
3. the current root `(device, object)` exactly equals `libraries.root_fs_device_id/root_fs_object_id`;
4. the target pathname remains beneath that exact registered root;
5. the immediate target parent is a real, non-reparse directory whose exact identity exists in `library_directory_identities`;
6. the target snapshot is identity-bound and, when present, single-link.

The sequence is:

```text
INITIAL
    registered Library root identity
    target-parent identity
    target snapshot

Phase-14 operational evidence hashing

FINAL
    registered Library root identity
    target-parent identity
    target snapshot

exact equality
    ↓
readiness fingerprint / optional SQLite record
```

The exact registered root path and filesystem identity are included in the readiness evidence fingerprint.

## Failure policy

If the current registered root identity differs from the persisted identity, readiness fails closed with a rescan/review-required error. A historically verified child directory cannot authenticate a substituted root merely because it has been moved beneath the same textual pathname.

## Recording

`record_target_replacement_readiness()` continues to rebuild readiness under `BEGIN IMMEDIATE`. Because the rebuild now performs both root attestations, a stale displayed readiness object cannot be immutably recorded after root substitution. No readiness row or readiness-recorded event is appended on failure.

## Authority

Unchanged:

```text
target_replacement_authorized = false
recovery_execution_authorized = false
```

No target create, replace, rename, delete, chmod, metadata repair, timestamp repair or EXIF write is introduced. Schema remains v40 with 40 migrations.

## Permanent regressions

14.2.2 adds two adversarial regressions:

1. rename the real Library root away, create a new root object at the old pathname, transplant the exact previously verified child directory into the fake root, then require both readiness build and recording to fail with no new row/event;
2. perform the same root substitution only after the initial topology/target checks, during Phase-14 evidence hashing, and require final root attestation to reject it.
