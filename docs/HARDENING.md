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
