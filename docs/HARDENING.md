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

## Phase 6 Slice 2 — cross-photo / sequence evidence (`chronology.py`)

Layers on frozen Slice 1 without rewriting it. Read-only, deterministic.

- **Clock-reset runs -> LIKELY_WRONG.** A maximal run of consecutive files (by
  filename sequence, which is independent of the clock) all timestamped at the
  SAME reset epoch with a forward-ticking clock, length >= `min_reset_run`
  (default 5), is treated as one camera-clock-reset event and its photos are
  escalated from QUESTIONABLE to LIKELY_WRONG. Short clusters and real bursts on
  non-reset days are NOT escalated (no false positives).
- **Timestamp regression -> downgrade.** A later-sequenced file with an earlier
  timestamp (beyond a 12h tolerance) is chronologically inconsistent; the
  out-of-order photo, if PROBABLY_VALID, becomes QUESTIONABLE. We can't prove
  WHICH is wrong, so it only adds doubt.
- **Grouping** is (library, directory, filename-prefix), so two cameras in one
  folder don't contaminate each other's chronology.
- **Escalation is licensed by independence** (filename order is independent of
  the clock); Slice 2 only adds doubt or confirms wrongness, never launders a
  doubtful date into a good one. `ppa chronology` reports runs/regressions.

Deferred to a later slice / Phase 7: genuinely-independent positive evidence
(user anchors, GPS time) that could yield the first TRUSTED dates, and actual
capture-date *reconstruction* (interpreting a corrected span from a reset run).
Tests: `tests/test_chronology.py` (9).

## Phase 6 Slice 2.1 — corrected cross-photo inference semantics

Governing principle: **filename sequence independently tells us about ORDER, not
calendar TRUTH.** Corrections to `chronology.py`:

- **Reset runs no longer escalate.** `reset_run` -> `reset_pattern`: a run of
  adjacent frames at one reset epoch with a forward clock is *reported* as a
  pattern consistent with a running reset clock, but reliability stays where
  Slice 1 put it (QUESTIONABLE). Order evidence corroborates "A before B", never
  "this calendar date is false". Escalation to LIKELY_WRONG is reserved for a
  later slice that adds independent evidence ABOUT THE DATE (trusted neighbour,
  camera-manufacture floor, user-confirmed event).
- **Camera-aware grouping.** Sessions group by (library, directory, camera_id,
  filename-prefix) using `files.camera_id` — two cameras that both name files
  IMG_* are not merged. Prefix is not assumed to identify a camera.
- **Adjacency segmentation.** Groups are split into segments by filename-sequence
  continuity (gap > `max_seq_gap`, default 10, or a backward jump breaks a
  segment), so IMG_0001/IMG_0100/IMG_5000 can't be one run and a folder spanning
  years isn't one session. Dates are deliberately NOT used to segment.
- **Order conflicts doubt both sides.** `timestamp_regression` ->
  `timestamp_order_conflict`, referencing both implicated file_ids; both drop
  PROBABLY_VALID -> QUESTIONABLE when the segment is a confirmed single camera.
  For unknown camera it's reported but not scored (may be interleaved cameras).
- **Still read-only; Slice 2 only ever adds doubt, never upgrades.**

Tests: `tests/test_chronology.py` (11), incl. the review's adversarial cases.
Escalation of a reset pattern to LIKELY_WRONG is deferred to a later slice /
Phase 7 (independent calendar evidence: trusted neighbours, camera floors,
user anchors), which is also where the first TRUSTED dates and capture-date
reconstruction will come from.

## Phase 6 Slice 2.2 — camera identity strength & sequence ambiguity

Principle: **`camera_id` means "camera metadata clustered these photos", not
"one physical body".** A serial-less make+model (e.g. two Canon A70s, serial
NULL) collapses to one `cameras` row, so it can merge distinct bodies.

- **Device-strength gate on order-conflict scoring.** `SequencedPhoto` carries
  `device_confirmed` (true iff the joined `cameras.serial` is present). A
  timestamp order conflict is: DEVICE_CONFIRMED -> reported AND both photos
  downgraded PROBABLY_VALID -> QUESTIONABLE; MODEL_ONLY / UNKNOWN -> reported but
  NOT scored (the "conflict" may be two different bodies). No schema migration —
  confidence is read from the joined camera row.
- **Duplicate filename sequence numbers are ambiguous order.** `_segment` now
  requires `1 <= delta <= max_seq_gap`; equal sequence numbers (delta 0) break a
  segment rather than being treated as adjacent frames.

Tests: `tests/test_chronology.py` (14), incl. two serial-less A70 bodies not
scored, a serial-confirmed body that IS scored, and duplicate-seq ambiguity.

## Phase 6 Slice 2.3 — device-identity strength (final Slice 2 correction)

Principle: **a present serial is not necessarily a meaningful unique serial.**
`00000000`, `UNKNOWN`, `N/A`, all-same-character strings, etc. are shared by many
bodies, so they must not license chronology scoring.

- `_is_strong_serial()` conservatively rejects absent/empty/placeholder and
  all-identical-character serials; only a credible unique serial counts.
- `device_confirmed` renamed `strong_device_identity` — epistemically honest:
  the archive has OBSERVED a purported EXIF serial, not independently confirmed a
  body. Order conflicts only downgrade under strong identity; otherwise reported,
  not scored.
- `analyse_sequence()` now sorts defensively by filename sequence, so a caller
  passing unordered input still gets correct segmentation.

Tests: `tests/test_chronology.py` (17), incl. two same-model bodies both emitting
`00000000` (reported, not scored), serial-quality unit checks, defensive sort.

### Slice 2 — ready to freeze
Layered model: filename sequence = ORDER evidence; camera/device identity strength
determines how strongly order conflicts may be interpreted; reset epoch = SUSPICION;
independent calendar evidence = later escalation (next slice / Phase 7).

## Phase 6 Slice 3.0 — independent calendar evidence (design + pure engine)

See docs/PHASE6_SLICE3_DESIGN.md. Storage-agnostic reconciliation core
(`reconcile.py`): layers a FINAL assessment over Slice-2 using ONLY evidence
independent of the camera clock that addresses the calendar date.

- **Escalation (earned LIKELY_WRONG):** candidate before a camera manufacture
  floor; outside a user anchor's exact date/range; or disagreeing with an
  independent GPS date.
- **Reset-run propagation:** one independent contradiction (or an exact anchor
  differing from the run's epoch) on any frame condemns every non-anchored frame
  of that clock-reset run.
- **Anchoring (first TRUSTED):** an exact user anchor (human ground truth, human
  date wins) or a GPS date that corroborates the recorded date. Nothing else
  yields TRUSTED; a lone contradicting GPS escalates but is NOT adopted as a
  trusted corrected date (reconstruction is Phase 7).
- Read-only, deterministic; with no Slice-3 evidence the Slice-2 result stands.

Deferred to 3.1: anchors table + migration, manufacture-floor config, GPS reader,
`analyse`/CLI integration. Tests: `tests/test_reconcile.py` (11).

## Phase 6 Slice 3.1 — independent-evidence persistence & wiring

- **Anchors table (schema v7, migration 007).** User-asserted calendar evidence
  (file/directory/library scope; exact/range), stored separately from
  observations — interpretation, resolved to photos at read time. `anchors.py`:
  add/list, and most-specific resolution (file > directory > library).
- **GPS reader.** `exif-gps:GPSDateStamp` ('YYYY:MM:DD', satellite-derived) read
  per current revision as independent calendar evidence.
- **Manufacture floors.** `camera_floors.py`: optional `(make|model) -> date` JSON
  config; EMPTY default (unknown model -> no floor -> no conclusion).
- **`analyse_library_reconciled(conn, camera_floors=...)`** runs Slice 1->2->3
  read-only, assembling GPS/anchor/floor evidence per photo and applying the pure
  reconcile engine (with its 3.0.1 provenance rules).
- **CLI:** read-only `ppa reconcile [--floors f.json] [--rating R]`; `ppa anchor
  add/list` (anchors are user interpretation, not originals/observations).

Tests: `tests/test_reconcile_catalogue.py` (8) — migration, anchor validation &
resolution precedence, GPS corroboration->TRUSTED, GPS contradiction->reset-run
condemnation, manufacture floor, exact-file-anchor precedence, read-only.

## Phase 6 Slice 3.2 — structured prior-doubt resolution

Answers the reviewer's deferred request: GPS can resolve a *specific* doubt
rather than blanket-trusting. Cross-layer:

- **Slice 1 emits structured `DoubtReason` codes** alongside free-text (RESET_EPOCH,
  ONLY_FILESYSTEM, FALLBACK_FIELD, FIELD_DISAGREEMENT, CONFLICTING_DUPLICATE,
  MTIME_PREDATES, MALFORMED), each flagged `resolvable_by_independent_date` or not.
- **Slice 2** adds `SequenceDoubt.ORDER_CONFLICT` (not resolvable) to downgraded
  photos; `PhotoChronology` carries the combined doubt list.
- **Slice 3** upgrades a QUESTIONABLE photo to TRUSTED on GPS corroboration ONLY
  when EVERY doubt is date-resolvable (RESET_EPOCH / ONLY_FILESYSTEM /
  FALLBACK_FIELD). Any unrelated doubt (field disagreement, order conflict,
  conflicting duplicate, mtime-predates) leaves it QUESTIONABLE, with the resolved
  and unresolved doubts both stated. QUESTIONABLE with no coded doubts is not
  upgraded (conservative). LIKELY_WRONG is still never resolved by GPS agreement.

So a reset-epoch photo whose GPS confirms the date becomes TRUSTED, but a photo
that is ALSO doubtful for an unrelated reason stays QUESTIONABLE — exactly the
"resolve the reset suspicion, leave unrelated contradictions intact" behaviour.

Tests: test_reconcile.py (+5), test_dating.py (doubt-code emission),
test_reconcile_catalogue.py (end-to-end GPS-resolves-reset-epoch).

## Phase 6 Slice 3.2.1 — group propagation gated on membership strength

Principle: **evidence may propagate across a group only when the evidence
establishing membership is strong enough to support the propagated claim.**
Slice 2's reset_pattern was harmless (no downgrade); Slice 3 made it actionable,
exposing that a serial-less model-only group can span multiple physical bodies.

- **Device-identity gate on reset propagation.** A reset group carries
  `reset_group_strong` (every member has a credible unique serial via
  `_is_strong_serial`). Whole-run condemnation from one independent contradiction
  is allowed ONLY for a confirmed single-device group; for a model-only/unknown
  group the contradiction applies to its own frame and the group merely stays
  suspicious.
- **Conflicting group evidence.** If independent evidence within one reset group
  both SUPPORTS and CONTRADICTS the shared date, propagation is withheld: frames
  with their own evidence keep individual results; frames without stay
  QUESTIONABLE with an explicit "group evidence conflicts" reason.

Tests: two serial-less same-model bodies (no propagation), placeholder-serial
group (no propagation), confirmed single device (one contradiction propagates),
support+contradiction in one group (withheld). Module header updated to 3.2.1.

---

# Phase 6 — Date Reliability Engine — COMPLETE & FROZEN

Slices 1 (intrinsic), 2 (cross-photo/sequence), and 3 (independent calendar
evidence) are accepted and frozen. Read-only, deterministic, layered; the enum
is never used as provenance; weaker signals never carry stronger claims.

Layered model, enforced in code:
  filename sequence      -> ORDER evidence (not calendar truth)
  camera/device identity -> how strongly order/reset evidence may be interpreted
  reset-epoch pattern    -> SUSPICION (never self-escalating)
  independent evidence   -> earned LIKELY_WRONG + first TRUSTED (anchors/GPS/floors)

`ppa reconcile [--floors f.json] [--export report.csv]` runs the full stack
read-only for reviewing against a real collection.

Next: **Phase 7 — Historical Date Reconstruction** (docs/PHASE7_DESIGN.md):
recover the actual capture date/range for flagged photos (clock-offset
propagation across confirmed reset runs, neighbour bracketing), as a separate
interpretation layer that never overwrites observations.

## Phase 7.0 — historical date reconstruction (pure engine)

`reconstruct.py`: storage-agnostic, read-only, deterministic. Produces an
interpreted capture date/range with confidence + evidence; never overwrites the
recorded date (interpretation stays separate from observation).

- **direct** — a frame's own independent true date (exact anchor / GPS) -> CONFIRMED.
- **offset** — a CONFIRMED single-device reset run has a wrong-but-monotonic clock;
  one known true date gives a day offset applied to the whole run (multi-day
  rollover handled) -> STRONG. Withheld for model-only groups and for conflicting
  datums. The marquee timeline-recovery step.
- **anchor_range** — a range/event anchor -> RANGE.
- **bracket** — a wrong frame between two point-dated neighbours (filename order)
  -> RANGE.

Tests: `tests/test_reconstruct.py` (9). Deferred to 7.1: reconstructions table +
migration, catalogue wiring, `ppa reconstruct` report, confirm/reject flow.
See docs/PHASE7_DESIGN.md.

## Phase 7.0.1 — reconstruction epistemics hardening

Rule: **a reconstruction may never be more precise or more certain than the
evidence that supports it.**

- **GPS never anchors an exact offset.** `GPSDateStamp` is UTC-derived while
  `DateTimeOriginal` is local/timezone-less, so a GPS date can be ±1 day from the
  true local date. GPS now reconstructs only its own frame as a ±1-day RANGE;
  offset propagation is anchored ONLY by an exact human/local date (typed
  `KnownTrueKind.HUMAN_EXACT`) belonging to the run.
- **Bracketing requires strong single-device ordering** (`reset_group_strong`);
  model-only groups may interleave two bodies, so filename order can't place a
  frame there.
- **Offset only revises QUESTIONABLE/LIKELY_WRONG** targets — never a clean claim.
- **Typed trust boundary:** `KnownTrueKind` enum; unknown confirmation sources,
  invalid ranges (end < start), and duplicate file_ids are rejected (ValueError).

Tests: `tests/test_reconstruct.py` (15), incl. Brisbane/UTC ±1-day non-propagation
and model-only bracket withholding. Deferred to 7.1: reconstructions table +
migration, wiring, `ppa reconstruct`, confirm/reject flow.

## UI — Library (resource) management

`catalogue.list_libraries` / `catalogue.forget_library` + `ui/libraries_dialog.py`
(a "Libraries…" toolbar action). The manager shows every source folder with live
present/missing counts, availability (offline drives shown amber), last-scan time,
and the current scan target (●). Actions: Add, Rescan, Set as Scan Target, Remove.

SAFETY: `forget_library` deletes only catalogue rows — never a source photograph.
Deletes are ordered around the files↔revisions cycle (null current_revision_id
first), orphaned photos are removed only when no File anywhere still references
them (a duplicate in another library keeps its Photo), and the whole thing is
atomic (rolls back on error). Verified: source files remain on disk, DB integrity
and foreign_key_check clean, no dangling revisions/observations.

Tests: `tests/test_library_management.py` (4), `tests/test_ui_smoke.py` (+2).

## UI — full-size photo preview

`ui/preview_dialog.py`. Double-click a grid tile (or press Enter/Return) to open
the selected photograph at full size, scaled to fit the window (aspect preserved,
smooth), with a caption (filename · dimensions · date · camera · copies) and a
position counter. Left/Right (or Prev/Next) navigate the current grid contents.

READ-ONLY: loads the original file's bytes only to display them (as thumbnails
already do); never writes to a source photo. Files that are missing on disk or
unreadable show a clear placeholder instead of crashing. Loading is gated on the
file actually existing on disk, not on a status string.

Tests: `tests/test_ui_smoke.py` (+2) — loads/navigates/clamps, and placeholder
for a file removed from disk after cataloguing.

## UI — preview performance & fidelity (self-initiated follow-up)

- **Bounded decode:** large photos are decoded at most to screen size via
  `QImageReader.setScaledSize` — a 24MP file no longer becomes a 24MP QPixmap
  just to display on a 2K screen (memory + speed).
- **EXIF orientation:** `setAutoTransform(True)` shows portrait photos upright.
- **LRU pixmap cache** (6 images) makes prev/next instant without holding the
  library in memory.
- **Loading indicator:** decode is deferred one event-loop turn so a brief
  "Loading…" paints for big files; a navigation token discards stale decodes.
