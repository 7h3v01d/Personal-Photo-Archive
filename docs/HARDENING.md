# Archive Core Hardening — findings, fixes, tests

Adversarial-review defects and where each is closed + regression-tested.

## Hardening 1–3 (Schema v1 → v3) — all closed

| # | Finding | Fix | Test |
|---|---------|-----|------|
| 1 | Multi-library: other library's file marked missing | Library scoping (`library_id`, migration 002) | `test_hardening_regressions::test_two_libraries_keep_both_active` |
| 2 | Byte-identical across libraries collapses catalogue | Library-scoped reconciliation | `…::test_identical_content_across_libraries_kept_separate` |
| 3 | Present-but-corrupt becomes missing | presence/health split; seen-paths set | `…::test_present_but_corrupt_is_not_missing` |
| 4 | Full previous SHA discarded on in-place change | full hash in event + immutable revisions | `…::test_full_previous_sha_is_preserved_on_content_change` |
| 5 | Stale in-memory hash index corrupts identity | index maintained on change; disk-path move/dup | `…::test_stale_hash_index_keeps_distinct_content_distinct` |
| 6 | Incomplete traversal marks files missing | `os.walk(onerror=…)` → fail closed | `…::test_incomplete_traversal_does_not_mark_missing` |
| 7 | Filesystem mtime observation goes stale | scanner owns + refreshes fs observation | `…::test_filesystem_mtime_observation_tracks_file` |
| 8 | Transient extraction failure recorded as success | `extraction_status` lifecycle; retry transient | `…::test_transient_metadata_failure_is_retried` |
| 9 | Stale camera_id survives content change | camera recomputed from current revision | `…::test_camera_id_cleared_when_content_loses_exif` |
| 10 | Metadata history destroyed across revisions | observations per `file_revision_id` | `…::test_metadata_history_preserved_across_revisions` |

## Hardening 3.1 (Schema v4) — interface fixes — all closed

| Finding | Fix | Test |
|---------|-----|------|
| Verify set `status=missing` but not presence/health | `verify_library` on presence/health | `test_hardening_31::test_verify_missing_sets_presence_not_just_status` |
| Hash mismatch left `health=ok` | verify sets `health_status='hash_mismatch'` | `…::test_verify_hash_mismatch_sets_health` |
| Verify backfilled file mirror but not revision (drift) | backfill updates authoritative revision too | `…::test_verify_backfill_updates_revision_not_only_file` |
| Scan fast-path ignored a verified mismatch | fast-path requires `health='ok'`; flagged mismatch held, not auto-promoted | `…::test_flagged_mismatch_poisons_scan_fast_path` |
| Historical revision re-extractable from current bytes | `extract_for_revision` refuses non-current | `…::test_cannot_reextract_historical_revision_from_current_bytes` |
| Filesystem observation history erased per file | fs observation replace scoped to revision | `…::test_filesystem_observation_history_survives_revision` |
| Crashed scan recorded as `complete` | session starts `running`, ends `failed` on crash | `…::test_crashed_scan_records_failed` |
| DB allowed cross-file revision/observation links | ownership triggers (migration 004) | `…::test_db_rejects_cross_file_current_revision`, `…::test_db_rejects_cross_owner_observation` |
| Library identity not Windows case-folded | `normcase(realpath(...))` canonical key | `…::test_library_canonical_key_is_case_folded` |

## Recorded design decisions / known limits

- **Cross-library identical files** are currently separate Photos. Target: same
  Photo, separate File, implemented as a **global duplicate-linking pass** — kept
  separate from library-scoped move reconciliation. Deferred, not yet built.
- **Flagged hash_mismatch is held**, not auto-accepted, until explicit
  reconciliation. There is not yet a reconciliation UI, so a flagged file stays
  flagged until resolved manually — intentional (preserves forensic value).

## Hardening 3.2 (Schema v5) — transaction & identity closure — all closed

| Finding | Fix | Test |
|---------|-----|------|
| Failed scan committed partial reconciliation | `conn.rollback()` before committing only the FAILED audit | `test_hardening_32::test_failed_scan_rolls_back_partial_reconciliation` |
| Failed scan left a new revision behind | same rollback; asserted no rev/sha/session/event drift | `…::test_failed_scan_does_not_commit_new_revision` |
| Within-library identity used raw absolute path (respelled root → phantom move) | identity = `library_id` + `relative_path_key` = normcase(normpath(relative_path)) | `…::test_respelled_library_root_is_not_a_move` |
| Overlapping library roots catalogued one file twice | fail closed on nested/containing roots (`commonpath`) | `…::test_overlapping_library_root_rejected` |
| Extractor version not recorded; version bump didn't invalidate | store `extractor_name`/`extractor_version` on the revision; stale when version differs | `…::test_extractor_version_bump_makes_extraction_stale` |
| catalogue reads used legacy `status` | converted to `presence_status` (authoritative); `status`/`files.sha256` remain maintained compat mirrors | (covered by existing catalogue/grid tests) |

## Additional recorded design decision

- **Content continuity ≠ photographic-identity continuity.** Same path +
  different bytes is treated as a new revision of the same Photo. This is
  usually right (an external edit), but an in-place *replacement* with an
  unrelated image would also be modelled as the same Photo. The revision ledger
  loses nothing, so this is deferred: later perceptual-similarity / user review
  can split a Photo when an in-place replacement is not the same subject.

## Hardening 3.2.2 (Schema v6) — filesystem-environment closure — all closed

| Finding | Fix | Test |
|---------|-----|------|
| Relative `files.path` → false "missing" after cwd change | store absolute (realpath) access path; refresh it on rescan | `test_hardening_32::test_files_path_is_always_absolute`, `…::test_relative_first_scan_survives_cwd_change` |
| Verify marked offline-library files missing | Verify is library-aware: unreachable root → `state='unavailable'`, files skipped, never missing | `…::test_verify_skips_unavailable_library` |
| `(library_id, relative_path_key)` not DB-unique | partial UNIQUE index scoped to present files (migration 006) | `…::test_duplicate_identity_rejected_by_db`, `…::test_deleted_then_recreated_path_does_not_violate_uniqueness` |
| Archive machinery could live inside a library (self-cataloguing loop) | scan rejects operational paths inside the library root | `…::test_archive_inside_library_rejected` |
| Dashboard counted historical mismatch events as current | `library_stats.hash_mismatches` = current `health_status`; `historical_mismatch_events` separate | `…::test_current_vs_historical_mismatch_counts` |
| Scanner still read legacy `status` for decisions | decision reads converted to `presence_status` | (covered by scanner + hardening suites) |

## Hardening 3.2.3 (Schema v6) — identity boundaries — all closed

| Finding | Fix | Test |
|---------|-----|------|
| Missing file returning to its exact path became a duplicate File | restoration ordered before duplicate; present-twin must be present | `test_hardening_323::test_missing_then_restored_same_path_reuses_file_id`, `…::test_exact_duplicate_still_detected` |
| File symlink inside a library resolved/catalogued outside it | containment check: every File must resolve beneath the library root; escaping links skipped | `…::test_file_symlink_outside_library_is_not_catalogued` |
| Library stayed `unavailable` after a successful scan | a successful scan sets `state='active'` | `…::test_library_state_recovers_active_after_successful_scan` |
| GUI startup selector came from raw `import_sessions.library_path` (could be relative) | recover from `libraries` (absolute canonical/display root) | `…::test_startup_library_selector_is_absolute_after_relative_scan` |

## Known limitation recorded for later (not blocking a local collection)

- **Removable-storage identity.** A Library is identified by its canonical path.
  If a different volume later occupies the same path (external drive swapped,
  drive letter reused), Verify sees the root as present and could mark the
  original photos missing. Mitigation for a future slice: record a volume/device
  identity (e.g. Windows volume serial) so "expected path present + wrong volume"
  reads as LIBRARY REPLACED / WRONG MEDIA rather than mass-missing. Fine for a
  fixed local library; matters before serious external-drive archive use.

## Hardening 3.2.4 (Schema v6) — operational boundary closure — all closed

| Finding | Fix | Test |
|---------|-----|------|
| Unavailable-root scan marked the library `active` (and a nonexistent path created a phantom Library) | root-availability pre-check: existing Library → `unavailable`, no `active`/`last_scan_at`; new absent path → `LibraryUnavailableError`, no Library row | `test_hardening_324::test_unavailable_root_scan_does_not_mark_active`, `…::test_nonexistent_root_creates_no_library` |
| Internal file symlink (alias of an in-library file) aborted the whole scan via the uniqueness constraint | alias detection: a dir entry resolving to an already-seen file is skipped (`alias_skipped`) instead of catalogued twice | `…::test_internal_symlink_alias_does_not_abort_scan` |
| `duplicate_files` counted missing historical copies as current | count only present copies (photos with >1 present file); the Duplicate view still shows historical relationships | `…::test_duplicate_files_counts_only_present_copies` |

---

# Archive Core Hardening — ACCEPTED (at 3.2.4)

Every finding across the review series is closed and fenced with a permanent
regression test (42 adversarial tests across six files). Source photographs are
never written. Foundation contract now holding under attack:

    Library -> File (persistent identity) -> FileRevision (immutable bytes)
    -> MetadataObservation (what those bytes said)

Next: Phase 6 — Date Reliability Engine (identify unreliable timestamps without
changing anything). Slice 1 (intrinsic per-photo signals) landed in `dating.py`.

## Phase 6 Slice 1.1 — date-engine epistemics (adversarial review)

Design principle adopted: **"multiple fields repeating the same claim are not
multiple witnesses."** Fixes to `dating.py`:

- Evidence is **source-qualified**: only an allow-list of `(source, key)` pairs
  is consulted; a non-EXIF value labelled `DateTimeOriginal` can't masquerade as
  EXIF. The pure API takes `DateObservation(source, key, value)`; a flat
  `{key: value}` dict is rejected.
- Intrinsic evidence **never yields TRUSTED** (reserved for independent evidence:
  human anchors, GPS time). Matching Original/Digitized or a nearby mtime is
  corroborating, not independent.
- **Contradiction never raises confidence**: disagreeing EXIF fields → QUESTIONABLE
  (with the disagreement as a reason); a corroborating mtime can't hide it.
- **Future** uses a timezone tolerance (+48h) so a timezone-less local time ahead
  of UTC isn't mislabelled.
- **Reset epochs** are suspicion (QUESTIONABLE), any time of day — not exact-midnight
  certainty; Slice 2 escalates with cross-photo evidence.
- **Malformed vs absent** distinguished via `DateSignal.status`.
- `best_estimate` renamed `candidate_date` (not an interpreted capture date).

Tests: `tests/test_dating.py` (19), including the review's eight adversarial cases.

## Phase 6 Slice 1.2 — chronology consistency & duplicate-evidence defence

- Filesystem mtime materially *before* the claimed capture time (a file can't be
  modified before it exists) downgrades PROBABLY_VALID -> QUESTIONABLE with a
  reason. Asymmetric: mtime *after* capture (copy/edit later) is normal and
  ignored. Generous 2-day tolerance absorbs timezone/clock noise.
- Conflicting duplicate `(source, key)` observations (same key, different values)
  are treated as ambiguous -> QUESTIONABLE, instead of silently adopting insertion
  order. Identical duplicates are redundant, not a conflict. The engine defends
  itself against imperfect input rather than trusting uniqueness upstream.

Adopted Phase 6 invariant: **repetition of a claim is not independent
corroboration.** (`DEFAULT_EARLIEST` remains policy/config, per-call overridable.)

## Phase 6 Slice 1.3 — ambiguous duplicate evidence (final Slice 1 cleanup)

- Duplicate `(source, key)` conflict detection now spans ALL observations, not
  just parsed ones: `valid + malformed` and `zeroed + valid` for the same key are
  ambiguous -> QUESTIONABLE (removing the contradictory "malformed, but no
  contradiction found" output). Identical repeats (same value, or same malformed
  raw) stay redundant, not conflicting.

### Slice 1 — FROZEN

The intrinsic single-photo Date Reliability Engine is complete and frozen. Its
epistemic hierarchy: UNKNOWN (no usable evidence) < QUESTIONABLE (usable but
doubtful) < PROBABLY_VALID (clean intrinsic evidence); LIKELY_WRONG for hard
impossibilities (far-future / before window); TRUSTED deliberately unreachable
from intrinsic evidence alone. Four standing rules: provenance is evidence;
repetition is not independent corroboration; contradiction cannot raise
confidence; intrinsic evidence alone never yields TRUSTED.

Next: **Slice 2 — Sequence / Cross-Photo Evidence** (filename/EXIF sequence,
neighbouring trusted images, reset-epoch runs) to escalate suspicions to
well-evidenced conclusions and reconstruct spans.
