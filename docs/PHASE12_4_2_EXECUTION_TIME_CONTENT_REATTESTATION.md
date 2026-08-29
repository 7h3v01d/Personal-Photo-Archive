# Phase 12.4.2 — Execution-Time Content Re-attestation

Phase 12.4.1 closed the semantic authority leak where an expected revision SHA
could masquerade as verified current bytes after Verify had already recorded a
mismatch.  A final adversarial pass exposed a narrower time-of-check/time-of-use
window: a File can be externally replaced **after** human review but **before**
a logical-Photo identity operation executes, while the SQLite catalogue still
contains healthy evidence for the old bytes.

Phase 12.4.2 closes that execution boundary.

## Authority rule

`verified_current_sha256` remains the canonical catalogue assertion that PPA has
healthy, coherent evidence for current bytes.  It is necessary, but an
identity-changing commit must additionally prove that the physical source still
reproduces that SHA at execution time.

For merge, split and recovery:

1. enter `BEGIN IMMEDIATE` and rebuild/revalidate the reviewed catalogue plan;
2. stably re-attest **every physical File whose current content participates in
   the identity decision**;
3. require each physical SHA to equal both the plan's verified-current SHA and
   the current FileRevision SHA;
4. perform the catalogue identity mutation;
5. re-attest the same physical Files again before commit; and
6. rollback on any mismatch, disappearance, unreadability or instability.

The second attestation prevents a source replacement during the identity
transaction from being hidden behind an otherwise valid database commit.

## Stable physical observation primitive

`ppa.physical_observation.observe_stable_image()` generalises the stable
read-only observation previously local to mismatch resolution:

- stat;
- SHA-256;
- Pillow decode/verify;
- SHA-256 again;
- stat again;
- compare size, nanosecond mtime, filesystem device/object identity and hash.

If those observations do not describe one stable object/byte stream, the result
is not positive identity evidence.  The caller fails closed and asks the user to
run Verify / refresh the investigation.

`ppa.mismatch_resolution` now uses this shared primitive while retaining its
existing mismatch-review semantics and error contract.

## Scope of re-attestation

### Controlled merge

Every physical File on **both** reviewed logical Photos is attested.  It is not
enough to hash only the cohort being reassigned because merge eligibility claims
that both Photos represent one current byte identity.

### Controlled split

Every physical File on the source logical Photo is attested.  The split depends
on the current partition of the complete File set into verified hash cohorts.

### Identity recovery

Every physical File on both the surviving source Photo and the split-created
Photo is attested before recombination.

### Exact-copy validation

`validate_exact_copy_pair()` now performs a fresh physical re-attestation of
both selected Files before returning them as proven current exact copies.  The
broader duplicate browsing projection remains catalogue-driven and read-only;
this execution/action validator is the authority gate.

## Source safety

Re-attestation opens photographs for reading only.  Phase 12.4.2 does not write,
move, rename, delete, repair, timestamp-touch or rewrite metadata in any source
photograph.  The only mutations remain catalogue identity/audit changes already
allowed by the reviewed operation, and those changes are rolled back if physical
evidence becomes stale.

## Database schema

No migration is required.  Catalogue schema remains **v31**.  The identity plan
schemas advance to reflect their stronger evidence contract:

- `ppa-identity-merge-plan/3`
- `ppa-identity-split-plan/3`
- `ppa-identity-resolution-review/3`
- `ppa-identity-recovery-plan/3`

## Regression contract

Permanent regressions cover:

- exact-copy validation rejects an external edit made without Scan/Verify;
- merge execution rejects an external edit made after planning;
- split execution rejects an external edit made after planning;
- recovery execution rejects an external edit made after planning;
- the shared stable observer rejects a source changing during its read cycle;
- an identity mutation is rolled back if a source changes between the pre- and
  post-mutation attestations.

The resulting rule is deliberately simple: **catalogue verification establishes
eligibility to review; execution-time physical re-attestation establishes
eligibility to commit.**
