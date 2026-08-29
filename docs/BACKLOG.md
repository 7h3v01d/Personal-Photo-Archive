# Backlog — deferred, non-blocking

Recorded from adversarial review at Archive Core Hardening sign-off (3.2.4).
None block Phase 6; they belong to later phases (forensics / Backup & Archive Health).

## 1. Ambiguous restoration among identical missing copies — CLOSED in Phase 12.2
Phase 12.2 no longer chooses one same-hash historical File by catalogue order.
When multiple absent-path candidates can explain a newly observed path, PPA
catalogues the observed object as a new File, preserves every candidate, and
records the complete ambiguity set in the append-only `file_origin_ambiguities`
ledger. Same-path restoration and a genuinely unique historical candidate retain
their existing deterministic behaviour. Filesystem object IDs are not used as
historical authority across absence because inode/file-index values may be reused.
See `docs/PHASE12_2_AMBIGUOUS_RESTORATION.md`.

## 2. Hard links look like independent duplicate copies — CLOSED in Phase 12.1
Phase 12.1 captures current filesystem device/object identity during normal scans
and Archive Health now detects when multiple catalogue paths share one filesystem
object. Distinct filesystem objects and distinct device IDs are reported
separately, while the UI/CLI still refuse to call either proof of independent
physical backup hardware. See `docs/PHASE12_1_STORAGE_IDENTITY.md`.

## 3. Thumbnail vs current-bytes after a hash mismatch — CLOSED in Phase 12.3
Phase 12.3 adds an explicit read-only expected/catalogued-vs-current forensic
comparison. Known mismatches cannot generate a new browsing derivative under the
trusted catalogue SHA, forensic expected derivatives require tamper-checkable
`ppa-thumbnail-attestation/1` provenance, legacy cache entries are labelled
unattested, and a separate current-byte derivative is keyed to the freshly
observed SHA. Migration 029 records structured Verify mismatch observations so no
forensic tool parses event prose for hash evidence. See
`docs/PHASE12_3_HASH_MISMATCH_FORENSICS.md`.
