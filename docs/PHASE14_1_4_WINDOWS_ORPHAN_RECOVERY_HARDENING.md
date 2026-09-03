# Phase 14.1.4 — Windows Orphan Recovery Hardening

## Why this patch exists

The Phase-14.1.3 adversarial pass accepted the POSIX descriptor-bound directory
mutation model but found a Windows liveness blocker.  Windows deliberately had no
pathname-based automatic cleanup fallback, so an interruption after
`expected-donor.<ext>` was installed but before its manifest/checkpoint completed
could strand the stage permanently: retry asked for reconciliation and
reconciliation could only say that safe deletion was unavailable.

The safety choice was correct; the lack of a safe forward path was not.

## Authority rule

Phase 14.1.4 does **not** add Windows deletion authority.  On a platform where
`BoundDirectory` cannot provide descriptor-relative mutation, donor-orphan
reconciliation switches from destructive cleanup to **verified orphan adoption**.

A final orphan may be adopted only when all of these are true:

- the Phase-14 preservation stage is still the committed stage object;
- the stage path contains no Windows junction/reparse component;
- no donor `.pending` or manifest `.tmp` debris exists;
- `expected-donor.<ext>` is a regular, non-reparse, single-link file;
- it is not the same filesystem object as the source donor or suspect target;
- it freshly decodes and reproduces the immutable expected SHA-256 and size;
- the original source donor freshly reproduces the same expected SHA;
- the target still matches the committed Phase-14 mismatch/missing observation;
- the current Phase-13 plan and latest human recovery intent still match;
- an existing orphan manifest is structurally/evidentially valid; if the manifest
  is missing, Phase 14.1.5 supersedes the original write path and embeds the
  canonical manifest in the append-only catalogue instead of creating it through
  an unbound Windows pathname;
- stage, orphan, donor, target and manifest are rechecked immediately before the
  immutable materialisation checkpoint is appended.

If any of those proofs fail, PPA leaves the operational debris untouched and
requires explicit manual intervention.  It never falls back to pathname deletion.

## Reparse/junction policy

Windows directory junctions and other reparse points are aliases even when they
present as directories.  Phase 14.1.4 therefore rejects Windows reparse points at
recovery, secure-write and thumbnail/cache authority boundaries.  Existing
reparse components in protected operational paths are rejected as well.  The
implementation uses `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT` and
`st_reparse_tag` without relying on Python 3.12's `Path.is_junction()`, preserving
Python 3.11 compatibility.

## CLI behaviour

`recovery-reconcile-donor-orphans <stage-id>` now means **reconcile safely**:

- POSIX/descriptor-capable platforms may remove proven uncheckpointed operational
  artifacts through the bound stage directory;
- Windows/unsupported platforms may adopt a fully verified final orphan;
- ambiguous/invalid debris is left untouched with a manual-intervention error.

A successful Windows adoption reports `orphan_artifact_adopted` and creates the
same immutable Phase-14.1 checkpoint as a normal materialisation.  It still does
not replace the target.

## Native Windows release gate

The repository includes Windows-only tests for:

- interruption after final donor installation but before checkpoint;
- retry/reconciliation adopting the verified orphan;
- stage-junction substitution rejection.

They are intentionally skipped on non-Windows runners.  Before freezing this
boundary, run on the normal NTFS Windows machine:

```bat
pytest -q tests/test_recovery_donor_materialization.py tests/test_windows_reparse_hardening.py
```

## Non-authority statement

Phase 14.1.4 still does not authorise target replacement, donor-to-target copying,
source rename/move/delete, EXIF writeback, or timestamp repair.  Phase 14.1.4 itself used schema v35; Phase 14.1.5 advances the current schema to v36 for embedded orphan-manifest evidence.
