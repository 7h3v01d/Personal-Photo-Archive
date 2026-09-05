# Phase 14.2.3.1 — Native-Windows Regression-Harness Compatibility

## Scope

Phase 14.2.3 is the adversarially accepted/frozen production implementation of bound destination topology finalization. Phase 14.2.3.1 changes **no production code**. It corrects two regression-harness assumptions exposed by the native Windows/NTFS full-suite gate.

## Windows findings

### Canonical source-directory lookup

`library_directory_identities.canonical_path` is stored using the project canonical form (`normcase(realpath(abspath(...)))`). The transplanted-known-parent regression previously queried that column with a raw `Path.resolve()` string. On Windows, case normalization can differ, causing the test to fail before exercising readiness. The regression now locates the known child by its exact `(fs_device_id, fs_object_id)` identity instead of by a platform-sensitive pathname string.

### Root rename while destination pins are live

The Phase 14.2.3 root-race regression assumed the registered Library root could always be renamed after the exact root and target-parent directory objects had been pinned. On native Windows/NTFS, the concurrently open directory pins can cause that ancestor rename to fail with `ERROR_ACCESS_DENIED` / WinError 5.

That is a stronger safe outcome, not a readiness failure: the substituted topology is never created. The regression now accepts the two platform-correct outcomes:

- POSIX / platforms where the rename succeeds: the post-hash `verify_pathname()` freshness check must reject the substituted topology.
- native Windows where the rename is denied by the live pins: readiness may continue only because the original topology remained unchanged and truthful.

The record-time variant follows the same rule: a successful substitution must not commit a stale row; a native Windows rename denial leaves the topology unchanged, so a normal truthful audit-only readiness row may be recorded.

## Invariants unchanged

- production `src/ppa/recovery_target_readiness.py` is unchanged from frozen Phase 14.2.3;
- `secure_write.py` and `WindowsDirectoryPin` are unchanged;
- root and parent are still pinned across the final target observation;
- root and parent pathnames are still reverified before readiness fingerprinting;
- `target_replacement_authorized = false`;
- `recovery_execution_authorized = false`;
- schema remains v40 with 40 migrations.

## Local regression gate

- Phase-14.2 readiness: 21 passed
- full repository: 697 passed, 7 skipped, 0 failed

The purpose of 14.2.3.1 is native-Windows test portability without reopening the accepted production authority model.
