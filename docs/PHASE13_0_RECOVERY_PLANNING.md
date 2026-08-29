# Phase 13.0 — Recovery Planning & Donor Qualification

Phase 12 is frozen. Phase 13 begins the recovery problem, but Phase 13.0 does **not** cross the source-photo write boundary.

Its job is to answer one question conservatively:

> After a human explicitly retained the immutable expected FileRevision and marked recovery as needed, does PPA currently have a donor File whose physical bytes can be proven to reproduce that exact expected revision, and what would a later recovery have to do?

## Authority boundary

A File enters recovery planning only when all of these remain true:

1. the target still owns the same unsuperseded expected FileRevision;
2. machine health remains `hash_mismatch`;
3. the **latest** human disposition for that same revision is `retain_expected_recovery_needed`;
4. current target bytes do not already reproduce the expected SHA-256.

A later unresolved/adopt decision supersedes the recovery-needed intent. If the original expected bytes have returned, recovery planning refuses and directs the user back to Verify.

## Donor qualification

A candidate donor is discovered catalogue-wide from Files whose current immutable revision SHA equals the target's expected SHA. Cross-Library donors remain byte donors only; qualification never merges or transfers logical Photo identity. It is qualified only when:

- it is present;
- `health_status='ok'`;
- the Phase-12.4.1 `verified_current_sha256` projection equals the expected SHA;
- it has no recorded unresolved `file_origin_ambiguities` evidence;
- a fresh Phase-12.4.2 stable physical observation reproduces the expected SHA;
- the source remains decodable and stable across stat → SHA → decode → SHA → stat;
- live filesystem topology does not show donor and target as the same filesystem object.

A catalogue-qualified donor that has been externally changed without Scan/Verify is therefore rejected by the physical observation layer.

## Storage topology

The planner reports one of:

- `distinct_filesystem_device_ids`
- `distinct_filesystem_objects_same_device_id`
- `target_storage_identity_unavailable`
- `donor_storage_identity_unavailable`
- `same_filesystem_object` (rejected)

These are current filesystem observations only. **Phase 13.0 never claims that different device IDs prove independent physical disks, enclosures, locations, controllers, or failure domains.** Every plan therefore carries:

```text
independent_backup_claim = false
```

Preferred-donor ranking is deterministic: different reported device IDs first, then different objects on the same device ID, then topology-unknown cases. Within equal topology strength, a donor already owned by the same logical Photo is preferred.

## Dry-run recovery plan

A plan is `ppa-recovery-plan/1` and is explicitly non-executable:

```text
dry_run_only = true
execution_authorized = false
```

It binds:

- target File/Photo/Library/path;
- donor File/Photo/Library/path and whether it is cross-Library;
- expected FileRevision + SHA-256;
- exact recovery-needed mismatch decision;
- fresh target physical state/SHA/size/mtime/device/object identity;
- donor File/Photo/path/current FileRevision;
- fresh donor SHA/size/mtime/device/object identity;
- topology classification;
- proposed future action sequence;
- SHA-256 evidence fingerprint.

For a mismatching or unreadable destination, the future action explicitly starts by preserving the suspect current bytes as recovery evidence before any replacement. For a missing destination it plans restoration to the recorded path. In all cases the later phase must stage donor bytes, prove the staged SHA, perform any eventual placement only under a separately reviewed execution contract, then run Verify before health may return to `ok`.

## Proposal audit

Migration 032 adds `archive_recovery_plan_proposals`.

Recording a proposal:

- revalidates target + donor evidence under `BEGIN IMMEDIATE`;
- appends the plan, topology, action sequence and fingerprint;
- appends an `archive_recovery_plan_proposed` integrity event;
- records `proposal_state='dry_run_not_executed'`;
- does **not** authorise recovery;
- does **not** alter target health or revision authority;
- does **not** write a source photograph.

Proposal rows are immutable during the Library lifetime. Deliberate Library-forget cascading remains possible under the existing archive lifecycle contract.

## Desktop workflow

After **Keep expected / recovery needed** becomes the latest mismatch disposition, the Hash Mismatch Investigation dialog exposes **Plan recovery…**.

The Recovery Planning dialog shows every candidate as **QUALIFIED** or **REJECTED**, includes rejection reasons and topology, and displays a preferred dry-run plan when one exists. The user may explicitly append that preferred proposal to the catalogue audit ledger.

## CLI

```text
python -m ppa.cli recovery-candidates <file-id>
python -m ppa.cli recovery-candidates <file-id> --json recovery-candidates.json

python -m ppa.cli recovery-plan <file-id>
python -m ppa.cli recovery-plan <file-id> --donor <donor-file-id>
python -m ppa.cli recovery-plan <file-id> --record --note "reviewed donor"
python -m ppa.cli recovery-plan <file-id> --json recovery-plan.json
```

`recovery-plan` remains a dry run even with `--record`; `--record` only appends the proposal evidence to SQLite.

## Source safety

Phase 13.0 source operations remain read-only:

- `Path.stat()`
- SHA-256 reads
- Pillow decode/verify

No source overwrite, copy-to-source, rename, move, delete, EXIF write, timestamp repair, or restoration path exists in this phase.

## Phase 13.1 boundary

Phase 13.1 may design the first actual recovery-write protocol. It must not simply "execute" a 13.0 plan. Any future write path must define and adversarially test staging, preservation of suspect bytes, destination race handling, atomic replacement semantics, Windows/network-filesystem behaviour, rollback/recovery after partial failure, post-write re-attestation, and Verify-owned health reconciliation.
