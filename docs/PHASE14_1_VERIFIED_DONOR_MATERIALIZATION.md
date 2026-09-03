# Phase 14.1 — Verified Donor Materialization

> **Hardening note (Phase 14.1.1):** the evidence/authority model below remains current, but all temporary writes are now descriptor-bound through `ppa.secure_write`; the older create-close-reopen pathname pattern is forbidden. Recovery checkpoint DELETE immutability, crash-orphan reconciliation, and wheel migration packaging are documented in `PHASE14_1_1_FILESYSTEM_WRITE_IDENTITY_HARDENING.md`.

Phase 14.0/14.0.1 created an immutable preservation checkpoint for the suspect target bytes. Phase 14.1 adds the next recovery boundary: copying the already-qualified expected donor bytes into that protected operational stage **without writing to the source target and without modifying the original donor**.

## Authority chain

A donor materialization may be planned only from one committed `archive_recovery_preservation_stages` row. The Phase-14.0 checkpoint remains immutable; Phase 14.1 appends a separate evidence record.

Before readiness is shown PPA:

1. reloads the committed preservation stage;
2. re-hashes its manifest and, when present, the preserved suspect bytes;
3. rebuilds the frozen Phase-13 recovery proposal;
4. requires the Phase-13 evidence fingerprint and recovery-intent decision to remain current;
5. freshly re-attests donor and target physical state;
6. requires the target to remain in the exact state preserved by Phase 14.0;
7. requires the donor to reproduce the immutable expected FileRevision SHA;
8. verifies sufficient operational free space;
9. binds all evidence into `ppa-recovery-donor-materialization-plan/1`.

The plan carries:

```text
materialization_authorized = false
target_replacement_authorized = false
recovery_execution_authorized = false
```

Planning is read-only.

## Explicit materialization

Materialization occurs only through a separate explicit action (`--apply` in the CLI or a desktop confirmation). Under `BEGIN IMMEDIATE`, PPA rebuilds the plan and then:

```text
fresh donor physical observation
        ↓
stream donor → sibling pending file
        ↓
fsync pending file
        ↓
verify streaming SHA/size == immutable expected revision
        ↓
independent readback SHA/size
        ↓
re-attest original donor after copy
        ↓
atomic install as expected-donor.<ext>
        ↓
fsync stage directory
        ↓
write + fsync donor-materialization manifest
        ↓
re-prove Phase-14.0 preservation evidence
        ↓
re-hash materialized donor + manifest
        ↓
re-attest donor + target immediately before commit
        ↓
append immutable Phase-14.1 checkpoint
        ↓
COMMIT
```

If donor, target, preservation evidence, or the new operational copy changes during the operation, the SQLite transaction rolls back and only the Phase-14.1-owned pending/materialized artifacts are removed. The already committed Phase-14.0 preservation checkpoint is never recursively cleaned or rewritten.

## Operational artifacts

Inside the existing stage directory, a successful Phase 14.1 materialization adds:

- `expected-donor.<original-extension>` — exact expected bytes copied from the freshly re-attested donor;
- `donor-materialization.json` — evidence manifest for this materialization.

Both are independently hashed before commit and made read-only on a best-effort, identity-bound basis after commit. The complete preservation root remains protected by `ppa.safe_export`.

## Database

Migration 034 adds immutable `archive_recovery_donor_materializations`.

Each successful row records a unique materialization ID, one unique Phase-14 stage ID, the Phase-13 and Phase-14 fingerprints, expected revision/SHA, donor source path, materialized path/SHA/size, donor manifest path/SHA, and explicit zero-valued replacement/execution authority flags.

A successful preservation stage may produce at most one verified donor materialization. A fresh human recovery review must create a new Phase-13 proposal and therefore a new Phase-14 stage.

## Source safety

Phase 14.1 adds no source-photo write authority.

Permitted:

- stat/read/hash/decode target and donor;
- read donor bytes;
- copy donor bytes into PPA operational recovery staging.

Still forbidden:

- target creation or replacement;
- donor modification;
- source rename/move/delete;
- EXIF/metadata writeback;
- source timestamp repair;
- any claim that donor staging itself authorises recovery execution.

## CLI

```text
python -m ppa.cli recovery-materialize-donor <stage-id>
python -m ppa.cli recovery-materialize-donor <stage-id> --apply
python -m ppa.cli recovery-materialize-donor <stage-id> --apply --note "verified donor staged"
python -m ppa.cli recovery-materialize-donor <stage-id> --apply --json donor-result.json
```

Without `--apply`, this is readiness only.

## Phase 14.2 boundary

Phase 14.2 may define a **target-replacement readiness protocol**, using only the committed preservation + donor-materialization evidence. It should still remain planning-only until replacement semantics, atomicity, destination-path authority, rollback, crash behavior, Windows sharing modes, and post-write verification have their own explicit contract.
