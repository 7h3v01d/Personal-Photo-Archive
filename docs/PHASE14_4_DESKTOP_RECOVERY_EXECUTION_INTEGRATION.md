# Phase 14.4 — Desktop Recovery Execution Integration

Phase 14.3 is adversarially accepted and frozen at **14.3.5**. Its final native
Windows/NTFS gate completed with **752 passed, 13 skipped, 0 failed**. Phase
14.4 therefore does not create another recovery authority model. It exposes the
frozen backend through the desktop while preserving the same explicit review,
authorization, one-shot, unresolved-attempt, and Verify-owned reconciliation
boundaries.

## Non-negotiable boundary

Phase 14.4 changes **UI/orchestration only**. It does not change:

- `recovery_target_execution.py` source-mutation semantics;
- `secure_write.py`;
- target/donor/preservation/readiness evidence rules;
- execution-attempt/result schema constraints;
- source-tree or operational authority;
- replay rules;
- Verify ownership of final catalogue health.

Schema remains **v41** and there is no migration.

## Desktop sequence

The desktop must not collapse readiness, audit recording and execution into one
button. The sequence is deliberately explicit:

```text
Phase-14.2 readiness build
        ↓
READ-ONLY / zero authority
        ↓
human chooses to record readiness
        ↓
record_target_replacement_readiness()
        ↓
immutable SQLite checkpoint only
        ↓
human chooses to review execution
        ↓
build_target_replacement_execution_plan()
        ↓
fresh execution UUID + fingerprint + exact confirmation phrase
        ↓
ZERO AUTHORITY PREVIEW
        ↓
human retypes exact phrase
        ↓
execute_target_replacement()
        ↓
frozen Phase-14.3.5 backend owns all mutation decisions
```

The execution button remains disabled until the typed phrase is exactly equal to
the phrase in the fresh plan. This UI gate is defense in depth only; the backend
still performs its own exact phrase, UUID, fingerprint and fresh-evidence checks.

## Failure / interruption presentation

A backend exception is not interpreted by the UI as proof that nothing changed.
The desktop immediately performs read-only `inspect_recovery_execution_status()`
for the exact execution UUID when possible.

If an attempt exists without a result, the UI must present it as **UNRESOLVED**
and display:

- execution ID;
- current target state;
- freshly observed target SHA-256 when safe/readable;
- retained suspect path when present;
- the original execution error.

It explicitly says automatic replay is blocked and does not offer a Retry action.
If no durable attempt exists, the UI reports a pre-attempt failure without
creating or inferring authority.

## Successful result presentation

`expected_target_placed_verified` is not presented as catalogue health repair.
The desktop states that expected bytes were placed and descriptor-verified, but
ordinary **Verify** must independently reconcile the File before health may
return to OK.

`aborted_exact_target_restored` is shown only when the frozen backend has already
performed its post-reverse-rename byte/identity attestation.

`aborted_before_target_transition` is shown only from the immutable backend
result; the UI never infers this state from an exception.

## New UI components

- `RecoveryTargetReadinessRecordWorker`
- `RecoveryTargetExecutionPreviewWorker`
- `RecoveryTargetExecutionWorker`
- `RecoveryExecutionStatusWorker`
- `RecoveryExecutionDialog`

All database/backend work remains off the GUI thread.

## Regression boundary

Phase 14.4 adds GUI regressions that require:

1. execution action disabled for blank/wrong confirmation text;
2. exact backend phrase enables exactly one emitted execution request;
3. the plan object and exact phrase are passed through unchanged;
4. readiness recording, preview, execution and status remain separate worker
   types rather than one authority-collapsing worker.

The existing Phase-14.3.5 backend and native Windows/NTFS regressions remain the
source-safety authority. Phase 14.4 must not weaken or duplicate them.
