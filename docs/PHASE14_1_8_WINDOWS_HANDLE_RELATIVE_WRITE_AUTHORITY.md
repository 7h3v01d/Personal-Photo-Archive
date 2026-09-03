# Phase 14.1.8 — Windows Handle-Relative Write Authority

## Purpose

Phase 14.1.7 correctly propagated expected parent-directory identity into every authority-bearing write, but its Windows strategy assumed that a native directory handle opened without `FILE_SHARE_DELETE` would reliably block the directory from being renamed while pathname-based writes were active. The native Windows 10 / NTFS gate disproved that assumption.

Phase 14.1.8 therefore removes pathname selection from Windows write authority rather than attempting stronger pre-write checks or stronger rename blocking.

## Windows authority model

1. Higher-level recovery/export/cache code supplies the already-authorised `(device, file-index)` identity.
2. `WindowsDirectoryPin.open()` opens the exact directory object and proves the native handle identity equals that expected identity.
3. Temporary children are created with `NtCreateFile` using the directory handle as `OBJECT_ATTRIBUTES.RootDirectory` and a single relative child name.
4. Existing-destination parking, final installation, rollback restoration, and cleanup use file handles plus `SetFileInformationByHandle(FILE_RENAME_INFO)` / `FILE_DISPOSITION_INFO`, relative to the same authorised directory handle.
5. The original lexical pathname is revalidated only as a liveness condition before reporting success. If it has been renamed/replaced, the operation fails closed and rolls back within the original directory object.

Thus a real directory can be renamed away and another ordinary directory can occupy its old pathname without receiving any PPA write.

## POSIX

POSIX keeps the Phase-14.1.7 design: `BoundDirectory` plus `dir_fd` child creation/install/cleanup.

## Native Windows regression

`test_windows_native_directory_handle_relative_child_survives_path_substitution` deliberately:

- opens authority over directory A;
- renames A away;
- moves an ordinary user directory B into A's old pathname;
- creates a new temporary child through the stored Windows directory handle;
- proves the child appears only inside renamed A;
- proves B and its user-owned sidecar remain byte-for-byte unchanged;
- proves lexical pathname verification detects that the requested path no longer names the authorised object.

This is stronger than asserting that a rename is blocked. Safety no longer depends on whether Windows permits the rename.

## Scope

No catalogue schema change is required; schema remains v36. No source-photo write authority, recovery-target replacement authority, or Windows orphan deletion authority is introduced. Phase 14.1.5 embedded orphan-manifest behavior and Phase 14.1.6 platform-correct cleanup policy remain unchanged.


## Native Windows correction note

The first 14.1.8 Windows implementation used `SetFileInformationByHandle(FILE_RENAME_INFO)` for the handle-relative rename half. The real Windows 10 gate returned `ERROR_INVALID_PARAMETER (87)` for that call, so 14.1.8 is not freezeable. Phase 14.1.9 retains the handle-relative authority design but moves rename to the matching NT-native `NtSetInformationFile(FileRenameInformation)` path.
