# Phase 14.3.2 — Native Windows Parked-Suspect Mutation Harness Correction

## Status

Pre-adversarial-review regression-harness correction. Schema remains **v41**.

## Trigger

The native Windows Phase-14.3.1 gate reached **747 passed, 13 skipped, 1 failed**. The sole failure was the regression intended to prove that a parked suspect whose bytes change is not restored merely for liveness.

The regression attempted to mutate the parked suspect by reopening its pathname with `Path.write_bytes()`. On native Windows that pathname reopen may itself be denied while PPA's exact-object handles are live. When that happened, no parked-object mutation occurred; the resulting `PermissionError` became the simulated pre-install failure, and PPA correctly re-attested the still-unchanged suspect and restored it. The test then incorrectly expected an unresolved attempt.

## Correction

The Windows regression now acquires an independent read/write handle to the target **before** Phase 14.3 execution. That external handle explicitly shares read, write and delete, so PPA may still perform its handle-relative parking rename while the external handle continues to identify the exact same filesystem object. During the simulated donor-copy stage, the test writes through that already-open handle, truncates and flushes it, and only then injects the failure.

This proves the intended interleaving:

1. external actor holds a writable, delete-sharing handle to the exact target object;
2. PPA independently opens and verifies the exact suspect target;
3. PPA parks that object by its own native handle;
4. the external handle mutates the same object while it is parked;
5. execution fails before donor installation;
6. rollback dual-view attestation detects the changed suspect;
7. PPA must leave the suspect parked and the durable execution attempt unresolved.

## Production boundary

No production source file changes in 14.3.2. In particular, `src/ppa/recovery_target_execution.py` and `src/ppa/secure_write.py` are byte-for-byte unchanged from 14.3.1. The 14.3.1 dual-view rollback rule remains authoritative.
