# Phase 14.1.9 — Windows NT-Native Rename Correction

## Trigger

The real Windows 10 / NTFS run of Phase 14.1.8 exposed one shared primitive defect and one test-environment resource problem.

- Handle-relative temporary child creation reached the Windows path successfully.
- `SetFileInformationByHandle(FILE_RENAME_INFO)` returned `ERROR_INVALID_PARAMETER (87)` during install/rename, breaking safe exports and thumbnail installation.
- The repeated full-suite runs used `%LOCALAPPDATA%\Temp`; that volume then exhausted free space, causing a large secondary cascade of SQLite, Pillow and pytest `tmp_path` failures. Those collateral failures are not treated as independent product defects.

## Correction

Phase 14.1.9 preserves the Phase-14.1.8 authority model but replaces the Windows rename half.

1. The higher layer authorises a concrete directory filesystem identity.
2. `WindowsDirectoryPin` opens and proves that exact directory object.
3. `NtCreateFile` creates the temporary child relative to that directory handle.
4. `NtSetInformationFile(FileRenameInformation)` renames/installs the open child relative to the same `RootDirectory` handle.
5. `FILE_RENAME_INFORMATION` uses the Windows 10+ 4-byte replace/flags union layout, native handle alignment, explicit UTF-16 byte length and sufficient trailing storage.
6. Lexical pathname revalidation remains only a liveness/reporting-success check; it does not select the directory that receives namespace mutations.

This keeps the central invariant: if the authorised directory is renamed and another ordinary directory later occupies the old pathname, PPA's temp creation and final installation remain attached to the originally authorised directory object.

## Test discipline

The native Windows gate should first run only `tests/test_windows_reparse_hardening.py::test_windows_native_directory_handle_relative_child_survives_path_substitution`. Only after that passes should the focused recovery/export/thumbnail set run, followed by the full suite. Full Windows runs should use a dedicated `--basetemp` on a volume with ample free space; do not use a nearly-full system `%TEMP%` as a correctness signal.

## Scope

Schema remains v36. No recovery evidence model, source-photo write authority, target-replacement authority, Windows orphan deletion authority, or POSIX filesystem design changes in this phase.
