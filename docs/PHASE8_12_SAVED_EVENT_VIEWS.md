# Phase 8.12 — Saved Event Views & Discovery Facets

Phase 8.12 adds durable, human-named **search presets** to Family History. A saved
view stores only the discovery recipe (query text, year/date bounds, and optional
occasion/place/people facets). It never stores matching Event IDs and therefore
cannot become a historical or membership snapshot.

## Authority boundary

Saved views are presentation metadata only. Saving, applying, updating, or deleting
a view cannot create/rename Events, change Event membership, edit Story Context,
change chronology, create anchors, alter reconstructions, write EXIF, or modify source
photos.

## Current filters

- free-text Event search (existing Phase 8.11 AND-token semantics)
- start year
- inclusive Event-span From/To dates
- occasion/context facet
- remembered-place facet
- people/group facet

Facet values are derived deterministically from the current Event search index.
People facets split only explicit comma/semicolon/newline-separated values; PPA does
not guess person identities from arbitrary prose.

## Saved-view semantics

A saved view is re-evaluated against the current Event index every time it is used.
If a newly-created or newly-edited Event now matches `Sydney`, an existing saved
`Sydney` view naturally finds it. Results are never cached inside the saved view.

Saved-view names are unique per Library, case-insensitively. Saving the same name
again updates that preset in place.

## Schema

Migration 017 adds `saved_event_views`. Rows are Library-owned and cascade away if
the Library is forgotten. The table stores query/filter intent only.

## CLI

```text
python -m ppa.cli event-views list <library_id>
python -m ppa.cli event-views save <library_id> "Christmas Events" --query christmas
python -m ppa.cli event-views save <library_id> "Sydney with Maddie" --place Sydney --person Maddie
python -m ppa.cli event-views run <view_uuid>
python -m ppa.cli event-views delete <view_uuid>
```

Phase 8.11 `event-search` also accepts `--occasion`, `--place`, and `--person`.
