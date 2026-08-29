# Phase 12.4.1 — Verified Current Identity Hardening

Phase 12.4.1 closes an authority leak exposed by the stronger Phase-12 integrity
model. Older Phase-10 identity code treated `files.sha256` as proof of the bytes
currently on disk. That assumption is no longer valid after Verify records a
`hash_mismatch`: the compatibility mirror deliberately continues to represent
the immutable expected/current FileRevision while the physical bytes are known
to disagree.

The release rule is now explicit: **logical Photo identity may use positive byte
identity only when current content is verified. Expected revision identity and
observed mismatch bytes remain separate evidence.**

## Three SHA meanings

PPA now uses a hard vocabulary distinction:

- **expected SHA-256** — immutable catalogue truth from the current
  `FileRevision` (with `files.sha256` retained as its compatibility mirror);
- **verified-current SHA-256** — a safe assertion about the File's current
  physical bytes; and
- **observed mismatch SHA-256** — forensic evidence recorded by Verify about
  bytes that disagree with expected authority.

The observed mismatch SHA is never substituted for verified-current identity.
When integrity is unresolved, current byte identity is **UNKNOWN**.

## Central verified-current primitive

`ppa.current_identity.verified_current_sha256_sql()` is the canonical projection.
It yields a SHA only when all of these conditions hold simultaneously:

1. the File is `present`;
2. `health_status='ok'`;
3. `current_revision_id` resolves to a FileRevision owned by that File;
4. that FileRevision has not been superseded;
5. the `files.sha256` compatibility mirror equals the revision SHA; and
6. the revision SHA is known and non-empty.

Missing, unreadable, hash-mismatched, incoherent or unhashed Files therefore
cannot contribute positive current-byte identity evidence.

## Phase-10 consumers converted

The verified-current projection now governs:

- exact duplicate grouping and exact-copy validation;
- current divergence detection and divergence investigation;
- competing logical-identity owner detection;
- controlled identity merge eligibility, plan fingerprints and execution;
- controlled identity split eligibility, plan fingerprints and execution;
- identity-resolution recovery eligibility and execution;
- equal-current-byte lineage guards; and
- Identity Health recommendations.

Identity Health gives unresolved integrity/availability the highest priority and
directs the user to resolve/re-verify it before merge, split, recovery or
exact-copy decisions. A stale expected SHA cannot create a false competing
identity or false current divergence.

Archive Health uses the same primitive for its current-content classifications.
Its structured schema advances to `ppa-archive-health/4`; a present mismatching
File has unknown verified-current SHA even while its expected SHA remains safely
recorded.

## Scanner boundary

Scanner's in-memory duplicate/reconciliation index now distinguishes two roles:

- a **present** File contributes to positive duplicate/current identity only when
  it has a verified-current SHA;
- a **missing** File may contribute a coherent expected current-revision SHA only
  as historical restoration evidence.

A present unhealthy File is never allowed to seed duplicate or relocation
identity from its stale expected hash.

Scanner also no longer clears a known mismatch merely because it re-observes the
expected bytes. It records `expected_content_reobserved_pending_verify` and leaves
the health state unchanged. This remains true when expected bytes reappear after
a missing/restoration transition. **Verify owns reconciliation from a known
integrity problem back to `ok`.**

## Merge/split/recovery stale-plan hardening

Identity-changing plans now fingerprint the evidence that makes current-byte
identity valid, including:

- expected SHA;
- verified-current SHA;
- current FileRevision id;
- presence state; and
- health state.

Execution re-creates the plan under `BEGIN IMMEDIATE`. A health transition or
loss of verified-current evidence therefore makes the reviewed plan stale before
logical Photo ownership can change.

## One-shot mismatch decisions — migration 031

Phase 12.4's non-authority-changing dispositions previously left all reviewed
evidence unchanged, allowing the same plan object to be replayed into duplicate
audit rows.

Migration 031 adds a unique `decision_id` to
`integrity_mismatch_resolutions`. Existing rows receive unique `legacy-*`
identities. New reviewed plans receive a fresh UUID; execution refuses a reused
decision id. Database triggers require the id and prevent it from being changed.

A genuine second human review remains append-only, but it must create a **fresh
plan / fresh decision identity**. One reviewed plan can produce at most one
forensic resolution record.

## Source safety

Phase 12.4.1 changes catalogue interpretation, query semantics, audit state and
scanner health reconciliation only. It does not write, move, rename, delete,
repair, timestamp-touch or rewrite metadata in source photographs.

## Regression contract

Permanent regressions use the real Scanner → Verify path and cover at minimum:

- a verified mismatch is not reported as an exact duplicate;
- exact-copy validation rejects a mismatching File;
- competing identity / merge reject unverified current content;
- a merge plan becomes stale when health changes after review;
- split planning rejects a mismatching File;
- a split plan becomes stale when health changes after review;
- recovery rejects unhealthy current content;
- the equal-hash lineage guard does not mistake stale expected SHA for current
  equality;
- Identity Health routes the case to integrity resolution rather than current
  duplicate/divergence advice;
- Archive Health does not call stale expected hashes current divergence;
- Scanner re-observing/restoring expected bytes leaves a mismatch for Verify to
  reconcile; and
- non-authority mismatch-resolution plans are one-shot.
