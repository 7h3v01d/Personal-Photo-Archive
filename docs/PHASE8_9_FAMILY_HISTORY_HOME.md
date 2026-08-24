# Phase 8.9 — Event Index / Family History Home

Phase 8.9 adds a read-only visual landing page over durable human Events.
It does not create Events, change membership, infer dates, or reinterpret chronology.

## Projection

`ppa.event_home.build_event_home(conn, timeline_view)` combines:

- the deterministic Phase 8.8 Event reading order;
- human Story Context;
- current Phase 8 Timeline lane counts;
- a semantically neutral default cover selection.

The output schema is `ppa-event-home/1`.

## Cover rule

The default cover is **not** a claim that a photograph is the best, earliest, most
important, or most representative image. PPA selects the lexicographically stable
`file_id` among visible `authoritative_seed` members. Only when no seed member is
visible does it fall back to the stable visible Event member ID.

Because the rule is identity-based rather than chronology-based, changing a photo's
date cannot silently change the cover. A later human-added member also cannot displace
an original seed simply because its file ID sorts earlier.

## Desktop UI

The main toolbar now includes **Family History**. Collection-wide Timeline + Event-card
projection runs on a worker with its own SQLite connection. The dialog provides:

- All Events and year-grouped navigation;
- Event cards with cover thumbnail, date span, member count, chronology lane counts,
  occasion/place context, and a short Story Context excerpt;
- at most 30 Event cards / cover decodes per page;
- selection detail;
- double-click or **Open story…** into the Phase 8.7/8.8 continuous Story View.

## CLI

```text
python -m ppa.cli event-home <library_id>
python -m ppa.cli event-home <library_id> --json family-history.json
```

## Authority boundary

Phase 8.9 performs no schema migration and no writes to Events, Story Context,
chronology evidence, reconstructions, metadata observations, EXIF, or source photos.
