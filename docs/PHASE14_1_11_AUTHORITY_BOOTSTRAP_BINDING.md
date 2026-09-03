# Phase 14.1.11 — Authority Bootstrap Binding

## Scope

Phase 14.1.11 closes the authority-bootstrap TOCTOU demonstrated against Phase 14.1.10. The previously hardened writer was correctly retaining authority over the exact directory object it had been given; the remaining defect was that PPA could select that object **after** a pathname-only safety decision and therefore faithfully bind the wrong object.

This phase changes the order of trust:

```text
old
path appears safe
→ later select directory object
→ that selected object becomes write authority

new
select/bind exact directory object
→ validate THAT object against archive policy
→ carry its identity/handle into every namespace mutation
```

The Phase-14 evidence model, donor qualification, immutable checkpoints, Windows orphan-adoption policy, and NT handle-relative file installation remain unchanged.

## Migration 037 — Library root filesystem identity

`libraries` now records:

- `root_fs_device_id`
- `root_fs_object_id`
- `root_fs_verified_at`

The pathname is location; these fields identify the verified Library root object. New/verified scans bind the Library root first and then persist its exact filesystem identity. Once both identity components are present, the database trigger `libraries_root_identity_no_rebind` prevents ordinary updates from silently redefining the Library as another filesystem object.

Existing upgraded rows remain NULL until a successful Library verification/scan. Authority-sensitive output/recovery operations treat NULL as **unknown** and fail closed rather than assuming path equality is sufficient.

## Shared directory bootstrap primitive

`ppa.secure_write` now exposes directory authority bootstrap across both platforms:

- `bind_directory_authority(path)` selects an existing exact directory object before policy validation.
- `ensure_directory_authority(path, validator=...)` binds the nearest existing ancestor and creates missing directory components relative to the already-bound authority.
- POSIX directory children are created with descriptor-relative `mkdir(..., dir_fd=...)` and rebound by descriptor.
- Windows directory children are created with `NtCreateFile` using the authorised directory handle as `OBJECT_ATTRIBUTES.RootDirectory`.

The validator runs on each selected object before that object can create the next namespace component.

## Safe export

`safe_export_temp()` no longer performs:

```text
validate path
mkdir
capture directory identity
validate path again
```

It now:

1. performs a non-mutating preliminary destination check;
2. binds/creates the output parent through `ensure_directory_authority`;
3. rejects the bound object if its filesystem identity equals any registered Library root identity;
4. re-evaluates path/topology policy while the same object remains pinned;
5. supplies that exact identity to `BoundTemporaryFile`.

A Library renamed onto the previously safe export pathname before binding is therefore rejected before temporary-file creation or destination replacement.

## Phase 14.0 preservation root

The preservation root itself is now authority-bound before stage creation. The UUID stage directory is created **relative to that bound root**; `Path.mkdir()` is no longer the stage-creation authority boundary.

If the preservation-root pathname is replaced after validation:

- POSIX creates only through the original root descriptor and fails freshness checks;
- Windows creates only through the original NT directory handle and fails freshness checks;
- the substituted source Library receives no stage directory.

Windows still does not gain generic pathname cleanup authority. If a safely-created operational child cannot be removed without weakening authority, operational debris may be retained rather than deleting by pathname.

## Thumbnail cache bootstrap

Thumbnail cache creation now uses the same bound-directory creation primitive rather than `mkdir → lstat → identity capture`.

The selected cache object is also required to look like PPA operational cache state:

- the Phase-14.1.11 cache marker `.ppa-thumbnail-cache-v1`, or
- legacy cache-shaped PNG/attestation entries, including marked nested forensic caches.

An ordinary source Library substituted onto the cache pathname before bootstrap is rejected before a thumbnail or attestation sidecar is written.

## Permanent adversarial regressions

Phase 14.1.11 adds attacks one operation earlier than the Phase 14.1.7 tests:

1. `safe_export`: swap the registered Library onto `exports/` after preliminary validation but before parent authority bootstrap. Result must be failure, source photo unchanged, source sidecar unchanged, and no output in the Library.
2. `ThumbnailCache`: swap a source Library onto the cache pathname before authority establishment. Result must be failure and zero new Library entries.
3. Phase 14.0: swap the source Library onto the preservation-root pathname before root binding. Result must be failure and no UUID stage in the Library.
4. Phase 14.0 post-bootstrap: swap the root after it has been bound but immediately before stage creation. Descriptor/handle-relative creation must not enter the replacement Library.
5. Native Windows: after a bound preservation-root pathname is replaced, `WindowsDirectoryPin.create_directory_child()` must not create the child in the replacement/source directory.
6. Scanner: a verified Library pathname that later names another filesystem object must fail closed, and the stored root identity must remain unchanged.
7. Database: established Library root filesystem identity cannot be changed by ordinary UPDATE.

## Schema and authority boundary

Schema advances to **v37**.

Phase 14.1.11 does **not** authorize source-photo writes, source deletion, metadata rewrite, timestamp repair, recovery-target replacement, or Windows orphan deletion. It only strengthens how operational write authority is selected before the already-hardened writer layer begins.
