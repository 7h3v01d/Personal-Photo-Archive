# Phase 8.8 — Event-to-Event Story Navigation

Phase 8.8 adds a deterministic, read-only reading order over durable human Events.
It does not create Events, alter membership, change Story Context, or influence chronology.

## Rules

- Events are ordered by `start_date`, then `end_date`, case-insensitive name, then stable Event UUID.
- Same-day Events therefore have deterministic order without relying on insertion time.
- Events are grouped for browsing by their **start year**. A multi-year Event remains one Event and is not duplicated across year groups.
- Previous/next links are derived from this immutable browse index.
- Story View reuses the already-authorised Timeline projection while moving between Events; navigation does not recompute or reinterpret dates.
- Event membership and Story Context remain human interpretation. Timeline lane/date remains independently governed by chronology evidence.

## Desktop

Story View now includes `Previous event` / `Next event` controls plus current year and position (`Event N of M`).

## CLI

```text
python -m ppa.cli event-browse <library-id>
python -m ppa.cli event-browse <library-id> --json events.json
```

Schema: `ppa-event-browse/1`.

## Authority boundary

Phase 8.8 performs zero source-photo, EXIF, metadata, reconstruction, anchor, Event, membership, or Story Context writes.
