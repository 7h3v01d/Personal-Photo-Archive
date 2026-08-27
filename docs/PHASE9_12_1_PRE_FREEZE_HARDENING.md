# Phase 9.12.1 — Pre-Freeze Adversarial Hardening

Phase 9.12.1 hardens the Phase-9 organisation stack before formal sign-off. It adds no new curation authority and no schema migration.

## Closed risks

### TOCTOU-safe membership undo

Organisation membership undo now acquires `BEGIN IMMEDIATE` before re-reading the target history row and proving that the action remains the latest membership state for the exact Album/Tag + logical Photo pair. The proof and inverse mutation therefore occur under the same reserved write transaction.

### TOCTOU-safe suggestion review

Assisted-organisation Apply and Dismiss now acquire `BEGIN IMMEDIATE` before rebuilding the current suggestion projection. The exact reviewed fingerprint is revalidated under the write lock before either Tag membership or dismissal state is changed.

### Bounded Organisation Activity queries

Activity undoability is resolved in bulk: one recent-ledger scan plus current Album and Tag membership sets. It no longer performs membership/latest-history queries per displayed audit row.

### Bounded unified discovery queries

Album and Tag memberships for a combined discovery recipe are fetched in at most one query per selector type. Selecting many Albums/Tags therefore no longer creates one membership SELECT per selector.

### Shareable-report privacy

The organisation report now sanitizes private paths and identifier-like material embedded inside human-authored names, descriptions, and notes. Recent activity prose no longer includes even shortened logical Photo IDs. The structured report continues to exclude archive paths, UUID object IDs, file IDs, hashes, thumbnails, database details, and source-photo content.

## Regression gates

Permanent regressions cover:

- write-lock-before-freshness for Undo;
- write-lock-before-freshness for suggestion Apply/Dismiss;
- bounded Activity SELECT count with a large history page;
- bounded unified-discovery SELECT count with many selectors;
- report redaction of embedded Windows paths, UUIDs, SHA-like hashes, and shortened Photo identity;
- the existing source-byte/mtime and evidence immutability gates.

No chronology, reconstruction, Event, EXIF, metadata-observation, or source-photo authority is introduced by this hardening pass.
