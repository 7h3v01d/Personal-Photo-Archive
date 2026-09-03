# Phase 14.1.2 — Recovery Commit-Boundary Hardening

Phase 14.1.1 closed the descriptor/path source-write bypass.  A subsequent
adversarial pass found two narrower consistency defects: exception cleanup could
still delete recovery evidence after SQLite had already committed the immutable
checkpoint, and donor-orphan reconciliation could race a concurrent materializer
because it decided "no checkpoint exists" outside a reserved writer transaction.

Phase 14.1.2 closes those transaction-boundary defects without adding any target
replacement authority.

## Commit-aware filesystem ownership

Phase 14.0 preservation and Phase 14.1 donor materialization now distinguish two
states explicitly:

```text
before durable checkpoint
    filesystem artifacts are rollback-owned

        ↓ SQLite COMMIT

after durable checkpoint
    filesystem artifacts are committed evidence
    rollback cleanup has no authority to delete them
```

The execution path tracks successful commit directly.  Exception handling also
covers the tiny interval where SQLite may have committed but an interruption is
raised before Python can set its local `committed=True` flag: if the connection is
no longer in a transaction, the immutable checkpoint is queried.  A visible
checkpoint is treated as durable authority and filesystem evidence is preserved.
If checkpoint status cannot safely be determined after SQLite has left the
transaction, cleanup fails closed toward evidence preservation.

This means a `KeyboardInterrupt`, `SystemExit`, or other `BaseException` during
post-commit read-only chmod/return housekeeping can no longer produce an
immutable catalogue checkpoint whose evidence files were deleted by rollback
cleanup.

## Serialized donor-orphan reconciliation

`reconcile_donor_materialization_orphans()` is itself an authority-changing
filesystem operation: it decides whether Phase-14.1 files are disposable crash
debris or committed recovery evidence.  It therefore now executes under:

```text
BEGIN IMMEDIATE
    ↓
load committed Phase-14.0 stage
    ↓
check donor checkpoint
    ↓
verify preservation evidence
    ↓
discover recognised orphan candidates
    ↓
RECHECK donor checkpoint
    ↓
unlink only if checkpoint still absent
    ↓
append reconciliation event
    ↓
COMMIT
```

`execute_donor_materialization()` already uses `BEGIN IMMEDIATE`; therefore the
materializer and reconciler are mutually exclusive at the catalogue writer
authority boundary.  A second connection cannot commit a materialization between
the reconciler's checkpoint decision and unlink.

## Secured-install aftermath

The descriptor-bound write primitive remains the source-byte authority.  Phase
14.1.2 additionally hardens the smaller destination-side install window:

- a pre-existing destination is parked at a random sibling rollback name;
- the secured temporary object is re-proved immediately before installation;
- POSIX retains the control descriptor through rename where supported;
- installed destination identity must equal the secured temporary identity;
- any failed/substituted install restores the previous destination (or the prior
  absence) before propagating the error.

A substituted pathname therefore cannot receive PPA writes **and** a failed
installation no longer leaves an existing export destroyed or replaced by an
alias to some unrelated object.

## Permanent regressions

Phase 14.1.2 adds tests for:

- Phase-14.0 interruption after durable commit but during post-commit housekeeping;
- Phase-14.1 interruption after durable commit but during post-commit housekeeping;
- interruption raised immediately after SQLite really commits but before the
  local commit flag can be updated;
- two-connection reconciliation/materialization interleaving proving the writer
  lock serializes authority;
- close/install-path substitution with an existing export destination, proving
  source bytes remain unchanged and the old export is restored.

## Authority boundary

Phase 14.1.2 remains operational-staging hardening only.

It does **not**:

- replace or create the source target;
- copy donor bytes into the source target;
- rename/move/delete source photographs;
- rewrite EXIF or timestamps;
- grant recovery execution authority.

Schema remains **v35**; no database migration is required for this hardening
slice.
