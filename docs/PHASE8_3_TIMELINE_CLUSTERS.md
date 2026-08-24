# Phase 8.3 — Timeline Context & Conservative Event Clustering

Status: implemented.

## Purpose

Phase 8.3 adds provisional chronological browsing clusters above the frozen
Phase-8 timeline placement model.  A cluster is navigation context, not new
historical evidence and not a named real-world event.

## Authority boundary

Only point-date items already in the authoritative `placed` lane may seed or
enlarge a cluster.  Tentative proposals and date ranges cannot create a cluster
or increase its authoritative photo count.  They may appear as contextual items
when their existing date/range overlaps a detected cluster interval.

No anchors, reconstructions, decisions, metadata, EXIF, database rows, or source
photos are changed.

## Conservative cluster shapes

### Same-day burst

At least four authoritative point placements share one calendar day.

### Dense multi-day run

Two through seven consecutive calendar days each contain at least two
authoritative point placements and the run contains at least eight photos in
total.

Runs longer than seven days are deliberately not promoted into one giant
pseudo-event.  Qualifying individual days may still appear as same-day bursts.

## Stable identity

Cluster keys are SHA-256-derived from:

- cluster kind;
- start date;
- end date;
- sorted authoritative member file IDs.

Ephemeral enumeration order does not participate in identity.

## Timeline UI

The Timeline scale selector now offers:

- Decades
- Years
- Months
- Clusters

A selected cluster shows authoritative members in the Placed lane and any
overlapping range/tentative items in their original lanes.  Range precision is
never collapsed to a point date.

## CLI

```text
python -m ppa.cli timeline-clusters <library_id>
python -m ppa.cli timeline-clusters <library_id> --directory 2001-2006
python -m ppa.cli timeline-clusters <library_id> --json clusters.json
```

Structured schema: `ppa-timeline-clusters/1`.

## Non-goals

Phase 8.3 does not infer labels such as birthday, holiday, trip, wedding, or
Christmas.  Human naming and richer evidence-backed event identity belong to a
later phase.
