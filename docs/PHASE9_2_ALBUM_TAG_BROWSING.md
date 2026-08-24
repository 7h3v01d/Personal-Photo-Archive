# Phase 9.2 — Album & Tag Browsing Views

## Status
Implemented after the accepted Phase 9.0 organisation model and Phase 9.1 desktop curation surface.

## Purpose
Turn a durable Album or Tag into a first-class, read-only photo-library view without changing the authority model underneath it.

## Core rules
- Album/Tag membership remains attached to logical `Photo`, never to a physical `File` copy.
- One logical Photo renders as one tile even when multiple physical copies exist.
- Rendering chooses one deterministic representative File: prefer a present copy, then filename (case-insensitive), then File ID.
- Missing-only logical Photo members remain visible as missing placeholders. Human curation is not silently erased when a source volume is offline.
- Browsing/filtering is presentation-only. It does not mutate membership, chronology, metadata observations, anchors, reconstructions, Events, EXIF, or source photographs.

## Browser projection
`ppa.organization_browse.build_organization_browse()` returns a versioned `ppa-organization-browse/1` immutable projection containing:
- object identity/kind/name/description;
- total logical members;
- members with at least one present File;
- missing-only members;
- one deterministic render/Preview File per logical Photo;
- a search corpus covering every filename for that Photo in the owning Library.

## Desktop UI
The Albums & Tags dialog now provides **Browse…** on both tabs and supports double-clicking an Album or Tag.

The browser provides:
- bounded thumbnail paging using the existing Phase-8 page size;
- asynchronous thumbnail decoding;
- filename filtering without database writes;
- current/total counts and present/missing-only counts;
- double-click/Open Photo into the existing read-only Preview;
- stable logical-Photo de-duplication.

## No schema migration
Phase 9.2 uses schema v19 unchanged.
