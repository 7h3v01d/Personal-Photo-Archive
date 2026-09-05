# Phase 14.1.17 — Exact Destructive Child Authority

## Purpose

Phase 14.1.17 applies the Phase 14.1.16 namespace invariant to destructive
cleanup. The previous build made installation and rollback non-destructive, but
adversarial review demonstrated that POSIX child cleanup still performed
`stat(name)` followed later by `unlink(name)` or `rmdir(name)`. A source object
could be substituted between those syscalls and be deleted.

Schema remains **39**. The positive-ownership model and the 14.1.16 atomic
installation design are unchanged.

## Security invariant

> No namespace mutation may replace or delete an object whose exact identity has
> not itself been authorised by the destructive operation.

Binding the parent directory is not enough. On POSIX, a directory descriptor
proves which namespace is being inspected, but a later `unlink(child-name)` or
`rmdir(child-name)` still acts on whatever object occupies that name at syscall
time.

## POSIX destructive cleanup

`BoundDirectory.unlink_child()` no longer deletes children automatically.
`BoundDirectory.remove_self_if_still_named()` no longer removes the stage entry.
Both operations validate their authority object but return without destructive
mutation.

This is deliberate. POSIX does not provide a general portable inode-bound
compare-and-delete primitive suitable for this threat model. Operational debris
is retained rather than granting pathname deletion authority.

## Directory-child creation failure

`BoundDirectory.create_directory_child()` no longer attempts `rmdir(name)` in
its exception cleanup path. A newly created directory can be renamed away and a
source directory substituted under the same child name before cleanup. The
created operational directory therefore remains as diagnosable debris when
exact deletion cannot be proved.

`ensure_directory_authority()` retains exact-handle cleanup on Windows, where a
newly created directory can be deleted through the native object handle. POSIX
validator failure retains the created directory.

## Recovery rollback cleanup

Phase-14.0 preservation rollback and Phase-14.1 donor rollback no longer invoke
POSIX child-name deletion. Failed stages may retain preservation files,
manifests, donor artifacts, and the stage directory itself. No catalogue
checkpoint is committed for the failed operation.

## Donor orphan reconciliation

Donor orphan reconciliation is now non-destructive on every platform:

- a fully verified final donor artifact may be adopted forward into immutable
  recovery evidence;
- a missing donor manifest is represented by canonical manifest bytes embedded
  in the append-only catalogue;
- temporary, invalid, or ambiguous debris is retained for manual intervention;
- no `archive_recovery_donor_orphan_reconciled` "removed" event is emitted for
  retained debris.

This removes liveness-oriented pathname deletion from the recovery authority
boundary.

## Permanent adversarial regressions

Phase 14.1.17 adds four release-gate attacks:

1. **Rollback child substitution** — a PPA rollback child is moved aside and a
   catalogued source photograph is placed under the same child name. Cleanup
   must leave the source bytes intact.
2. **Orphan reconciliation substitution** — a catalogued source object occupies
   an expected donor temporary name. Reconciliation must retain it, require
   manual intervention, and emit no false removal event.
3. **Stage-directory substitution** — a historically recorded source directory
   is moved under the operational stage name. Stage cleanup must return false
   and leave that exact source directory intact.
4. **Directory-child creation failure** — after a newly created child directory
   is moved aside and a replacement directory appears under its name, exception
   cleanup must not `rmdir()` the replacement.

## Development gate

Final segmented repository gate for this build:

- **671 passed, 6 skipped, 0 failed** across all 75 test modules;
- high-risk recovery/output/hardening gate: **140 passed, 5 skipped**;
- modified recovery/output surfaces: **95 passed, 2 skipped**;
- schema remains **39**.

## Release note

Phase 14.1.17 should be adversarially reviewed before freeze. Reviewers should
attack every remaining destructive namespace operation and verify that POSIX
cleanup retains debris instead of deleting by a previously checked child name.
Native Windows exact-handle deletion should be tested separately on NTFS.


## 14.1.17.1 Windows gate correction

A native Windows full-suite run exposed two release-gate issues that did not
change the destructive-authority design:

- the POSIX-only `BoundDirectory.create_directory_child()` substitution test
  was missing its Windows skip guard;
- the final Windows `replace=False` native rename correctly rejected a late
  destination with WinError 183, but `_install_windows()` leaked that raw OS
  exception instead of normalizing it into `SecureWriteError`.

14.1.17.1 adds the platform guard and normalizes WinError 80/183 at the final
no-replace install boundary. No overwrite fallback is introduced and the late
destination remains untouched. Schema remains v39.


## 14.1.17.2 — Source Object Exclusion for Orphan Adoption

Adversarial review of 14.1.17.1 found that a byte-valid final donor orphan could
still be the exact filesystem object previously catalogued as source evidence.
Byte equality proves revision equivalence; it does not confer operational
ownership.

14.1.17.2 adds a single source-authority exclusion boundary. Before automatic
orphan adoption, PPA compares the candidate `(device_id, object_id)` against both
the current `files.fs_device_id/fs_object_id` values and
`file_storage_identity_history`. History is joined back to the owning File, so
explicit Library forgetting naturally retires that authority through existing
cascade semantics.

The same rule applies to a pre-existing `donor-materialization.json`. Its exact
validated filesystem identity is retained and re-proved immediately before the
checkpoint commit; a same-bytes object substitution cannot inherit the earlier
manifest validation.

A source-associated donor or manifest fails closed and remains untouched:

- no operational adoption;
- no donor-materialization checkpoint;
- no `archive_recovery_donor_orphan_adopted` event;
- no chmod/read-only mutation;
- manual intervention required.

Permanent regressions cover current source identity, historical source identity
after a replacement inode is observed at the source pathname, and the existing
filesystem-manifest equivalent. Schema remains v39.


## 14.1.17.3 — UI Authority Startup Gate

The source-tree authority gate is intentionally stricter than older catalogues:
a registered Library with no verified root/source-tree identity must be rescanned
before writable operational output is permitted.  That safety state is not,
however, a reason for the read-mostly desktop UI itself to fail to start.

Thumbnail worker construction now distinguishes **authority unavailable pending
rescan** from an unsafe cache object.  Pending authority creates a disabled
worker: it performs no cache creation or derivative write and exposes the rescan
requirement to the UI.  The main window remains usable for scanning and ordinary
catalogue navigation, and a successful complete scan re-arms thumbnail service.

This does **not** weaken `SourceTreeAuthorityPolicy`, operational-directory
ownership, or cache-object validation.  Any cache-path/object safety failure still
fails closed.  Schema remains v39.
