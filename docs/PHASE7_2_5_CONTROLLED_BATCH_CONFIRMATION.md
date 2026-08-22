# Phase 7.2.5 — Controlled Batch Confirmation

Phase 7.2.5 adds a deliberately narrow batch-authority workflow for reconstructed
camera-clock reset runs. It does not add inference. It only allows one human review
action to confirm multiple already-generated proposals when the complete group can
prove a simple, stable provenance chain.

## Eligibility

A batch is offered only when all of the following hold:

- the target belongs to a strong single-device reset run;
- the run contains at least three photographs;
- every member has a current stored reconstruction;
- every member remains `proposed` and fresh against both bytes and evidence;
- every result is a point date, never a range;
- reconstruction methods are only `direct` (the exact human anchor) or `offset`;
- confidence is `confirmed`/`strong`;
- exactly one exact human-anchor frame is the calendar basis.

Ambiguous-device runs, partial groups, bracket/range results, stale rows, or already
made decisions are not batch eligible.

## Human review

The UI displays five distributed visual samples where available: first, quarter,
middle, three-quarter, and last. Image decoding occurs off the Qt GUI thread. The
user must explicitly acknowledge reviewing the samples before the commit control is
enabled.

## Atomic authority

A `BatchPlan` freezes each member's file id, proposed date, source revision and
evidence fingerprint into a batch token. Immediately before commit PPA rebuilds the
entire plan. Any difference aborts the operation.

The decision update runs in one SQLite transaction. If any member cannot be updated
under the frozen revision/evidence preconditions, the transaction rolls back. There
is no partial batch confirmation.

Each photograph still receives its own ordinary `confirmed` reconstruction decision;
there is no group-level truth record and no source/EXIF write.
