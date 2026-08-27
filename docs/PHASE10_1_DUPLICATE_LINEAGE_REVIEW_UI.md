# Phase 10.1 — Duplicate & Lineage Review UI

Phase 10.1 exposes the Phase-10.0 identity model in the desktop without adding any automatic merge, split, deletion, winner-selection, or inferred-lineage behaviour.

## Review lanes

The **Duplicates & Lineage** surface deliberately separates three questions:

1. **Exact Copies** — current physical File records that share both one logical Photo identity and one known current SHA-256. Two File rows can be opened in a read-only side-by-side viewer only after a core guard re-proves same Library, same logical Photo, and same non-null current SHA-256.
2. **Identity Divergence** — one logical Photo whose represented Files currently carry more than one known SHA-256. Divergence is a review warning only; PPA does not automatically split or repair the Photo identity.
3. **Photo Lineage** — active human-confirmed directed relationships between distinct logical Photos. The desktop can add or remove these relationships using the Phase-10.0 audited APIs.

## Side-by-side preview

The comparison viewer decodes the two selected physical files with EXIF auto-transform and scales them for display. It does not write source bytes, metadata, thumbnails, hashes, catalogue identity, or chronology.

## Lineage curation

The Add dialog offers only logical Photos represented in the current Library and the fixed Phase-10 relationship vocabulary. Creation still passes through the core guards: self-links, cycles, unsupported relationship types, duplicate parent/child edges, and byte-identical distinct Photo identities fail closed.

Removing a lineage relationship removes only the active edge. Append-only `photo_lineage_history` remains intact.

## Authority boundary

Duplicate review and Photo lineage remain orthogonal to chronology, reconstruction, Event membership, Albums, Tags, metadata observations, EXIF, and source-photo bytes.

Schema remains **v23**. No migration is required for Phase 10.1.
