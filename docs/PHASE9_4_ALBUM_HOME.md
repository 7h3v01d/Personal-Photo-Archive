# Phase 9.4 — Album Home / Visual Album Library

Phase 9.4 adds a read-only visual landing page for Albums. It is the Album-side equivalent of Family History Home, but it consumes only Phase-9 organisation/presentation state.

## Invariants

- One card per durable Album, including empty Albums.
- Album counts are logical Photo counts, not physical File counts.
- Human preferred cover wins when present; otherwise a stable logical-Photo ID provides a deterministic presentation-only default.
- Cover rendering chooses a deterministic representative File, preferring a present copy.
- Custom Album order does not implicitly become the Album cover.
- Missing-only logical Photos remain part of counts and browsing.
- Search matches only Album name and description.
- Maximum 30 Album cards are rendered per page; only visible cover thumbnails are decoded.
- Album Home is read-only and cannot change Album membership, chronology, metadata observations, anchors, reconstructions, Events, EXIF, or source files.

## CLI

`python -m ppa.cli album-home <library_id>`

Add `--json <path>` for schema `ppa-album-home/1` output.
