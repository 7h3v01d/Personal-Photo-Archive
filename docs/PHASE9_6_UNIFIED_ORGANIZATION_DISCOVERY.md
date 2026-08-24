# Phase 9.6 — Unified Organisation Discovery

Phase 9.6 adds one read-only discovery surface spanning explicit Album and Tag
membership. A query is an exact intersection over logical Photo IDs.

Examples:

- `Album: Holidays ∩ Tag: Beach`
- `Album: Holidays ∩ Tag: Beach ∩ Tag: Family`
- `Album: Family Favourites ∩ Album: Prints`

There is no fuzzy matching, semantic inference, chronology inference, or hidden
OR behaviour. Every selected Album/Tag is a required set constraint.

## Invariants

- Queries operate on logical Photos, never physical File copies.
- Duplicate Files therefore render as one result tile.
- All selected objects must belong to the requested Library.
- Missing-only Photos remain durable members and render as placeholders.
- Result browsing reuses the Phase-9.2 bounded browser and Preview path.
- Discovery is read-only and cannot alter Albums, Tags, Events, metadata
  observations, anchors, reconstructions, EXIF, chronology, or source photos.
- Initial selector construction and result calculation run on worker-owned
  SQLite connections off the Qt GUI thread.

## CLI

```text
python -m ppa.cli organization-discovery 1 \
  --album <holidays-album-id> \
  --tag <beach-tag-id> \
  --tag <family-tag-id>
```

Use `--json result.json` for the structured `ppa-organization-discovery/1`
result.

No schema migration is introduced in Phase 9.6; schema remains v20.
