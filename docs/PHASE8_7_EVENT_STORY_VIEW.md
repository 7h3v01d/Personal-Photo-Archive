# Phase 8.7 — Event Browse / Story View

Phase 8.7 turns durable human Events into an album-like, read-only browsing surface.
It does **not** introduce a new chronology source.

## Authority boundary

The view deliberately presents three independent facts:

1. **Event identity / membership** — explicit human interpretation.
2. **Story context** — human-authored narrative memory.
3. **Current Timeline state** — the only source used for chronological placement.

A sentence in Story Context can never place a photo. A human-added Event member that
is `UNPLACED` remains unplaced. A date range remains a range. Tentative chronology
remains tentative.

## Ordering

Visible Event members are ordered by their current Timeline chronology. Members with
no defensible current date are placed at the end of the story rather than assigned a
synthetic date.

## UI

From Timeline's **Events** scale (or a named Cluster), use **Story view…**.

The Story View shows:

- human Event name and saved date span;
- occasion, remembered place, people, description, and story text;
- bounded chronological thumbnail pages (120 photos maximum at once);
- current lane/date for each member;
- membership role (`authoritative_seed` or `human_added`);
- chronology provenance on demand;
- direct Preview opening.

Thumbnail decoding remains off the Qt GUI thread.

## CLI

```text
python -m ppa.cli event-story <event-uuid>
python -m ppa.cli event-story <event-uuid> --json story.json
```

JSON schema: `ppa-event-story/1`.

## Safety

Phase 8.7 performs no schema migration and no writes to Events, chronology,
metadata, EXIF, or source photographs. It is a presentation/read-model slice.
