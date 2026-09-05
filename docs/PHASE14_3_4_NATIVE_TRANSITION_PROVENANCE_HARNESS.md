# Phase 14.3.4 — Native Transition-Provenance Regression Harness

Phase 14.3.4 is a **test/documentation-only compatibility correction** on top
of the Phase-14.3.3 Target Transition Provenance implementation.

No production recovery or secure-write semantics change in this revision.

## Why the correction was required

The Phase-14.3.3 regression for an install-internal failure after target-name
acquisition reproduced the reviewer's exact POSIX ordering by monkeypatching:

1. `BoundDirectory.rename_child_noreplace_atomic()`; then
2. `BoundDirectory.fsync()`.

That is correct on POSIX. Native Windows missing-target installation does not
use either primitive; it uses `WindowsDirectoryPin.rename_fd()` inside
`BoundTemporaryFile._install_windows()`. Consequently the POSIX fault injection
never fired on Windows and the otherwise-correct restore completed normally.

## Correct cross-platform regression

The single provenance regression now selects the actual platform boundary:

### POSIX

`RENAME_NOREPLACE` succeeds -> target name acquired -> bound parent `fsync()`
fails.

Required durable state:

- execution attempt present;
- execution result absent;
- execution unresolved;
- acquired target remains present with the immutable expected SHA-256.

### Windows

Native handle-relative `rename_fd()` succeeds -> target name acquired -> the
next internal `WindowsDirectoryPin.verify_pathname()` is fault-injected.

The Windows secure-write rollback may prove and remove that exact newly
installed object. Even when rollback succeeds, the durable attempt must remain
**unresolved** because target acquisition did occur and must never be rewritten
as a pre-transition abort.

Required durable state:

- execution attempt present;
- execution result absent;
- execution unresolved;
- target absent after proven native rollback.

This directly exercises `SecureWriteTransitionError` on both platform-specific
installation paths without weakening or changing the Phase-14.3.3 production
implementation.
