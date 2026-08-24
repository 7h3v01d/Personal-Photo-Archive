# Phase 9.5 — Tag Home & Organisational Discovery

Phase 9.5 gives explicit human Tags a first-class visual landing page and adds exact Tag intersections.

## Invariants

- Tags remain Library-owned, human-applied organisational metadata.
- Intersections are exact set intersections over logical `Photo` identities.
- A logical Photo renders once even when multiple physical copies exist.
- Cross-Library intersections fail closed.
- Missing-only members remain represented rather than being silently dropped.
- Tag names and intersections never become chronology/date evidence.
- Tag Home and intersection projections are read-only.

## Desktop

The main toolbar now includes **Tags**. Tag Home supports bounded 30-card paging, name search, cover thumbnails, single-Tag browsing, and multi-selection intersection browsing. Intersection results reuse the Phase-9.2 logical-photo browser and its 120-photo page bound.

## CLI

- `ppa tag-home <library_id>`
- `ppa tag-intersection <library_id> <tag_id> <tag_id> [...]`

Both support structured JSON output where applicable.
