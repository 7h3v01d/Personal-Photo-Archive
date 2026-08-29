# Phase 14.0 — Recovery Execution Protocol & Preservation Staging

Phase 13 is frozen at 13.0.1. It proves that a human still wants the immutable expected revision recovered and that a physically re-attested donor exists. Phase 14 begins recovery execution **one authority boundary at a time**.

Phase 14.0 does **not** restore a photograph. It answers a narrower question:

> Before any later replacement is even considered, can PPA preserve the exact suspect target bytes into protected operational storage while proving that the frozen recovery proposal, donor, target and preservation copy all still match the reviewed evidence?

## Authority boundary

Phase 14.0 accepts only a **recorded** Phase-13 proposal from `archive_recovery_plan_proposals` whose state remains `dry_run_not_executed`.

Before presenting preservation readiness it:

1. reloads the immutable Phase-13 proposal;
2. rebuilds the current recovery plan using the same target, donor and proposal ID;
3. requires the rebuilt Phase-13 evidence fingerprint to equal the recorded fingerprint;
4. requires the same current recovery-needed human decision;
5. freshly re-attests target and donor physical bytes again;
6. requires donor SHA-256 to equal the immutable expected FileRevision SHA;
7. refuses if the target has already returned to the expected bytes;
8. derives a new Phase-14 preservation-stage fingerprint.

A Phase-14 plan explicitly carries:

```text
execution_authorized = false
target_replacement_authorized = false
donor_materialization_authorized = false
```

The plan alone is read-only and creates no preservation directory.

## Explicit preservation execution

Preservation occurs only after explicit execution (`--apply` in the CLI or a separate desktop confirmation).

Execution runs under `BEGIN IMMEDIATE` and rebuilds the complete Phase-14 plan before writing anything. If the Phase-13 or Phase-14 fingerprints are stale, execution fails closed.

For a present mismatching or unreadable target:

```text
frozen Phase-13 proposal
        ↓
rebuild + compare catalogue evidence
        ↓
fresh target + donor physical re-attestation
        ↓
validate operational preservation root
        ↓
check available space + safety reserve
        ↓
stream target bytes → sibling pending preservation file
        ↓
fsync pending file
        ↓
compare streaming SHA/size to reviewed suspect evidence
        ↓
independently re-hash preservation file
        ↓
re-attest source target after copy
        ↓
atomically install preservation copy
        ↓
write + fsync manifest
        ↓
re-attest target + donor again
        ↓
append immutable catalogue checkpoint
        ↓
COMMIT
```

Any mismatch or unstable observation removes the ordinary in-process staging directory and rolls the SQLite transaction back.

## Preservation storage

The default root is:

```text
<catalogue-directory>/recovery-preservation/<stage-id>/
```

It is PPA operational state, not a source Library. The root:

- must resolve outside every registered source Library;
- may not itself be a symbolic link;
- must be a directory if it already exists;
- is protected from ordinary `ppa.safe_export` user-directed output paths.

Each successful present-target stage contains:

- `suspect-source.<original-extension>` — exact source bytes observed as suspect;
- `manifest.json` — Phase-13/14 fingerprints, source/donor observations, preserved SHA/size and explicit non-authority flags.

Both files are made read-only on a best-effort basis after the catalogue commit. Correctness depends on SHA-256 evidence and immutable catalogue rows, not filesystem permission bits.

## Missing target

If the target is already missing, there are no suspect bytes to preserve. Phase 14.0 therefore records:

```text
target_missing_no_preservation_required
```

and writes only the operational manifest/checkpoint. It does **not** create an empty stand-in source file and does not restore the target.

## Database

Migration 033 adds immutable `archive_recovery_preservation_stages`.

A successful row records:

- one unique `stage_id`;
- one unique frozen Phase-13 `proposal_id`;
- target/donor/revision/recovery-intent identities;
- Phase-13 and Phase-14 plan fingerprints;
- target/donor physical observations;
- preservation root/path and preserved SHA/size when applicable;
- manifest path + SHA;
- stage state;
- `target_replacement_performed=0`;
- `donor_materialized=0`;
- `recovery_execution_authorized=0`;
- final evidence fingerprint and optional note.

A frozen proposal may produce at most one successful preservation stage. A genuine re-review must produce a fresh Phase-13 proposal rather than replaying the old preservation authority.

## Source safety

Phase 14.0 is the first phase that writes **source-derived** bytes, but it still performs no write *to a source photograph*.

Permitted source operations remain:

- `stat()`;
- byte reads / SHA-256;
- Pillow decode/verify;
- read-only streaming of current suspect bytes into PPA operational preservation storage.

Still forbidden:

- donor → target copy;
- target overwrite/replacement;
- missing-target creation;
- source rename/move/delete;
- source EXIF/metadata writeback;
- source timestamp repair.

## CLI

```text
python -m ppa.cli recovery-stage-preservation <proposal-id>
python -m ppa.cli recovery-stage-preservation <proposal-id> --apply
python -m ppa.cli recovery-stage-preservation <proposal-id> --apply --note "preserved before recovery"
python -m ppa.cli recovery-stage-preservation <proposal-id> --apply --json preservation-result.json
```

Without `--apply`, the command is only a readiness/evidence view.

## Desktop workflow

After a Phase-13 proposal is successfully recorded, the desktop offers a **separate confirmation** to stage the suspect bytes. The confirmation states explicitly that a preservation copy will be written to PPA operational storage but the source target will not be replaced and donor bytes will not be copied.

The worker owns its SQLite connection and performs staging off the GUI thread.

## Crash/interruption scope

Ordinary Python/IO/database failures are cleaned up and rolled back. A process or machine crash can theoretically leave an uncommitted `.pending`/stage directory in operational storage. Such a remnant is outside every source Library and has no catalogue recovery authority. Reconciliation/cleanup of crash remnants is intentionally deferred to a later Phase-14 operational-hardening slice rather than silently treating them as successful evidence.

## Stage-path and rollback-cleanup authority

A Phase-14 stage identifier is an opaque **canonical UUID**, not caller-controlled path text.  The execution API revalidates it before filesystem use, so values such as `..`, path separators, absolute paths, or other non-UUID text cannot escape the preservation root.

After PPA creates the stage directory it captures that directory object's filesystem identity.  Failure cleanup is bound to that exact object and removes only artifacts PPA itself created.  It does **not** recursively traverse arbitrary stage contents and does not chmod unexpected aliases/symlinks.  If unexpected content prevents `rmdir`, the remnant is left for later diagnosis rather than risking cleanup outside the operational stage.

Successful custom preservation roots recorded in the catalogue are also treated as protected operational trees by the archive-safe export layer.

## Phase 14.1 boundary

Phase 14.1 may consider materialising **donor bytes into recovery staging**, but still should not replace the source target until that donor-staging boundary has its own explicit plan, stable-copy proof, interruption semantics and adversarial review.
