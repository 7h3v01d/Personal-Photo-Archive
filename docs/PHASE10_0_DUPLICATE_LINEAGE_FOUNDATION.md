# Phase 10.0 — Duplicate Identity & Copy-Lineage Foundation

Phase 10 begins by separating two concepts that must not be conflated.

## Exact duplicate

Byte-identical physical Files are already represented by one logical `Photo`.
Phase 10.0 exposes that existing identity as a deterministic, read-only duplicate view.
A current exact-copy set additionally requires equal known current SHA-256 values. If Files
under one logical Photo have diverged into multiple known current hashes, PPA surfaces an
**identity divergence** for review rather than falsely calling them exact duplicates or
automatically splitting the Photo. It does not create a second Photo, merge anything,
delete anything, or infer similarity.

## Photo lineage

A lineage relation connects two **different** logical Photos when a human knows that one
is derived from another. Supported initial relations are:

- `derived_copy`
- `edited_variant`
- `resized_variant`
- `format_conversion`
- `crop`
- `unknown_derivative`

Lineage is human-confirmed in Phase 10.0. Cycles and self-links are prohibited, including
at the SQLite layer. Distinct Photos with equal current SHA-256 values are rejected because
byte-identical content belongs under one logical Photo identity instead of a lineage edge.

Removing a lineage edge preserves append-only lineage history.

## Authority boundary

Duplicate/lineage state is not chronology evidence and cannot modify metadata observations,
anchors, reconstructions, Events, Albums, Tags, EXIF, or source-photo bytes.
