# Phase 14.3.3 — Target Transition Provenance

## Status

**Adversarial correction / review required.**

Phase 14.3.3 closes the post-install provenance defect found during adversarial
review of the first source-mutating recovery boundary. The defect was not that
wrong bytes were installed; it was that a generic secure-write exception could
span both sides of the atomic destination-name acquisition and therefore let the
execution ledger falsely record `aborted_before_target_transition` after the
expected target name had already been acquired.

Schema remains **v41**. No new source-mutation capability is added.

## Provenance invariant

Phase 14.3 must distinguish these two states exactly:

```text
NO TARGET TRANSITION PROVEN
```

from:

```text
TARGET NAME ACQUIRED / TRANSITION OCCURRED
```

A generic exception is never proof of the first state.

## Secure-write boundary

POSIX bound no-replace rename is now split conceptually into:

```text
renameat2(RENAME_NOREPLACE)
        ↓
TARGET NAME ACQUIRED
        ↓
record transition provenance immediately
        ↓
parent-directory fsync
        ↓
post-install exact-object verification
```

`BoundDirectory.rename_child_noreplace_atomic()` performs only the atomic
no-replace namespace mutation. `rename_child_noreplace()` then performs the
parent-directory durability step. If that durability step fails after the
atomic rename succeeded, secure-write raises `SecureWriteTransitionError`
rather than an undifferentiated `SecureWriteError`.

`BoundTemporaryFile.install()` preserves that transition provenance across its
own exception/rollback boundary. The same exception type is also used when a
Windows destination name was acquired before a later secure-write failure.

## Phase-14.3 result rule

For missing-target restore:

```text
atomic no-replace refuses acquisition (for example EEXIST)
        ↓
proven pre-transition abort
        ↓
aborted_before_target_transition
source_namespace_changed = 0
```

But:

```text
atomic target acquisition succeeds
        ↓
any later durability / identity / verification step fails
        ↓
NO execution-result row
        ↓
attempt remains UNRESOLVED
```

The execution attempt was already durably committed before physical mutation,
so leaving the result absent is the truthful crash/recovery state.

## Permanent regressions

Phase 14.3.3 adds two required regressions using the real missing-target
execution path:

1. atomic `RENAME_NOREPLACE` succeeds, the target contains the immutable
   expected bytes, then the immediate parent-directory fsync fails;
2. install succeeds and the expected target is present, then the post-install
   exact-object verification boundary fails.

Both must prove:

- one immutable execution-attempt row exists;
- zero execution-result rows exist;
- read-only status reports `resolved = false`;
- the target exists and matches the immutable expected SHA;
- no `aborted_before_target_transition` checkpoint is written.

The existing late-arriving-target regression remains unchanged: when the atomic
no-replace syscall itself refuses acquisition, the external occupant survives
and the attempt may truthfully resolve as a pre-transition abort.

## Review focus

Adversarial review should now fault-inject every boundary after target-name
acquisition, especially directory durability and exact-object verification, and
confirm that no such failure can be resolved as `aborted_before_target_transition`.
