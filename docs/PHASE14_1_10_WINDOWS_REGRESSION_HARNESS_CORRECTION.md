# Phase 14.1.10 — Native Windows Regression Harness Correction

## Scope

Phase 14.1.10 is intentionally a regression-harness correction. It does **not** change production filesystem authority, recovery evidence, schema, or the Windows NT-native create/rename design introduced in 14.1.8 and corrected in 14.1.9.

## Native 14.1.9 result

The real Windows 10 full suite collected 680 tests and completed with **667 passed, 11 skipped, 2 failed**. The failures were:

1. `test_failed_install_substitution_restores_existing_export_destination` still attacked the historical `os.rename` installation boundary. Windows now installs through `WindowsDirectoryPin.rename_fd`, so the attack hook was never reached.
2. `test_windows_native_directory_handle_relative_child_survives_path_substitution` correctly created the child inside the renamed original directory object, but attempted a second pathname open while the native child handle was still live. Windows sharing semantics rejected that reopen with `PermissionError`.

Neither result demonstrated source modification, destination redirection, or failure of handle-relative placement.

## Corrected regressions

### Safe-export rollback

- POSIX retains the original descriptor-relative hard-link substitution attack.
- Windows injects a failure at the actual native `rename_fd` final-install boundary, after any previous destination has been parked.
- The operation must fail through `ArchiveOutputSafetyError`.
- The previous export must be restored byte-for-byte.
- The registered source file must remain unchanged.

### Native directory substitution

The Windows-native test now:

1. opens authority on directory object A;
2. renames A to `stage.parked`;
3. moves an ordinary replacement directory into A's old pathname;
4. creates the temp child relative to A's open handle;
5. verifies exact bytes through the open child descriptor;
6. verifies child namespace membership through A's directory handle;
7. renames the child to `installed.txt` relative to A's directory handle;
8. verifies the replacement directory received no child;
9. closes the child descriptor;
10. then verifies `stage.parked/installed.txt` by pathname.

This tests the intended authority model without depending on Windows allowing concurrent pathname reopening of a handle whose share mode may forbid it.

## Schema

Unchanged: **v36**.

## Release gate

Before freeze, run the corrected native Windows tests on real NTFS, then the focused recovery/export set, then the complete suite.
