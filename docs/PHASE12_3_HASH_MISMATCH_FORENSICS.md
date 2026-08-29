# Phase 12.3 — Hash-Mismatch Forensics & Trusted Derivative Boundary

Phase 12.3 closes the final deferred Archive Core adversarial backlog item: after
Verify proves that a present File no longer hashes to its immutable current
`FileRevision`, PPA can now inspect **expected/catalogued image evidence** and
**current untrusted on-disk bytes** without collapsing one into the other.

## The trust problem

The ordinary thumbnail cache is keyed by the catalogue SHA-256. Historically,
a cache miss could decode whatever bytes currently occupied the File path. If a
silent external change had already occurred, those changed bytes could therefore
be rendered into a derivative whose filename contained the *old trusted hash*.
The health flag still warned the user, but the cache key itself was not forensic
provenance.

Phase 12.3 separates ordinary browsing convenience from forensic evidence.

## Known mismatches cannot create catalogue-keyed browsing thumbnails

`GridItem` now carries current `health_status`. Thumbnail requests for a File in
`hash_mismatch` state may reuse an existing cached derivative but **may not create
a new cache entry under the expected SHA key**. This rule applies to the main
catalogue, Event covers, Timeline/Event Story grids, and bounded organisation /
Archive Health browsers that use the shared thumbnail worker.

This avoids making the known-mismatch state worse while preserving fast browsing
of pre-existing cache entries.

## Attested derivatives

Forensic use has a stricter contract than ordinary browsing.
`ThumbnailCache.get_or_create_attested()`:

1. hashes the source and requires the exact expected SHA-256;
2. renders the derivative only after that check;
3. re-hashes the source after rendering to fail closed if bytes changed during
   materialisation;
4. hashes the generated PNG itself; and
5. writes an atomic sidecar using schema `ppa-thumbnail-attestation/1` containing
   the source SHA, derivative SHA, render size and attestation time.

An existing derivative is considered attested only when its sidecar validates and
the current derivative hash still matches the recorded derivative hash.

Legacy thumbnails created before Phase 12.3 remain valid browsing cache entries,
but are **not** called trusted forensic references merely because their filename
contains the catalogue hash.

## Expected-image recovery

For a mismatching File, PPA establishes the left-side expected image in this order:

1. an already valid attested catalogue-keyed cache entry;
2. if the current bytes now reproduce the expected SHA, re-attest those bytes as
   the expected reference (without clearing database health);
3. a different present `health=ok` File whose current revision claims the expected
   SHA, but only after that copy is re-hashed *now* and proves the expected bytes;
4. an old catalogue-keyed cache as **legacy/unattested context**; or
5. no expected image at all.

PPA never regenerates the expected image from bytes that currently mismatch the
expected revision.

## Current-byte preview

The right side is generated in a separate `thumbnails/forensic-current` cache,
keyed and attested to the SHA-256 computed from the bytes currently on disk. It is
explicitly labelled observation, not archive authority.

If the file changes again between Verify and investigation, the UI preserves both
the last Verify-observed SHA and the fresh investigation SHA and reports that the
bytes changed again.

## Structured mismatch evidence — schema migration 029

`integrity_events` remains the human-readable append-only event ledger. Migration
029 adds `integrity_mismatch_observations` so forensic code never needs to parse
prose to recover the actual hash seen by Verify.

Each observation records:

- File id;
- expected immutable FileRevision id;
- expected SHA-256;
- observed SHA-256;
- observed path;
- observed size and filesystem mtime nanoseconds; and
- observation timestamp.

Repeated Verify mismatches append repeated observations. They are history, not a
mutable resolution record.

## Desktop investigation

Selecting a File whose health is `hash_mismatch` now exposes **Investigate hash
mismatch…** in the inspector. The worker hashes/decodes off the GUI thread and the
read-only dialog shows:

- expected/catalogued reference and its provenance class;
- current on-disk preview and current SHA;
- the most recent structured Verify-observed SHA/time; and
- explicit notes when evidence is unavailable, legacy/unattested, revalidated, or
  changed again after Verify.

The investigation cannot repair, accept, overwrite, rename, delete, merge, split,
or otherwise mutate a source photo or catalogue identity.

## Authority boundary

Finding that current bytes now match the expected SHA does **not** clear
`health_status`. The investigation is read-only with respect to the catalogue;
only a subsequent Verify may reconcile current health back to `ok`.
