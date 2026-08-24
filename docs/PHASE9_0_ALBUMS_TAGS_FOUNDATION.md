# Phase 9.0 — Albums & Tags Foundation

Phase 9 introduces a second organisational axis beside chronology and Events.

## Authority boundary

Albums and Tags are **human-authored organisation**, not photographic evidence.
They never alter or become inputs to:

- metadata observations;
- chronology reliability;
- anchors;
- reconstructions;
- Timeline placement;
- Event date spans;
- source-photo bytes or embedded metadata.

A label such as `Christmas 2004` or `25 December 2004` is therefore searchable
organisation only. It is not a date claim accepted by the chronology engine.

## Logical Photo membership

Album and Tag membership targets `photos.id`, not `files.id`.

This preserves the established model:

`Photo` = logical photographic identity

`File` = one physical copy of that photograph in a Library

Two duplicate physical copies of the same logical Photo therefore produce one
Album membership and one Tag association rather than duplicated curation.

## Library ownership

Albums and Tags are Library-owned. SQLite triggers require a Photo to have at
least one File in that Library before it can be added/tagged. This is enforced
both in the Python API and at the database boundary.

If the same logical Photo genuinely exists in two Libraries, each Library may
organise it independently.

## Durable audit

Schema v19 adds:

- `albums`
- `album_photos`
- `tags`
- `photo_tags`
- `organization_history`

Album create/rename/description/member changes and Tag create/rename/apply/remove
changes are append-audited. Repeating an already-applied membership operation is
idempotent and does not manufacture duplicate history.

## Phase 9.0 scope

This slice is the persistence/API/CLI foundation. It intentionally does not yet
add album ordering, covers, nested collections, tag hierarchy, bulk desktop
curation, or organisation search. Those can build on the stable v19 model.
