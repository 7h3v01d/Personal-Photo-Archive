# Phase 10.2 — Identity Divergence Investigation

Phase 10.2 adds a read-only forensic projection for a logical Photo whose current physical Files carry different known SHA-256 values.

It reports each File's current state, first/last catalogue observation and immutable `FileRevision` chain. Classification is deliberately limited to:

- `modified_in_place`: PPA itself observed more than one distinct revision hash for at least one physical File.
- `distinct_when_first_observed`: PPA first observed different physical Files with different hashes and has no evidence of an in-place change. This does **not** prove derivation, originality, or that Photo identity should be split.
- `insufficient_evidence`: revision evidence is incomplete.

The investigation never merges/splits logical Photos, creates lineage, deletes files, chooses an original, changes chronology/evidence, or writes source photos.
