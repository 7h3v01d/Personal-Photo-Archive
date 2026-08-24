# Phase 8.2 — Timeline Scale & Fast Jumping

## Purpose

Make the provenance-aware Timeline practical for a multi-decade archive without
changing any chronology authority established by Phase 8.0.

## Contract

Phase 8.2 is presentation/navigation only. It does not create or modify dates,
anchors, reconstructions, decisions, metadata, EXIF, or source photographs.

Density buckets are counts, not evidence. A range may be indexed by its start
month/decade for navigation, but remains a range everywhere else.

## Scale model

The immutable `TimelineView` can be aggregated at three stable scales:

- decade (`1990s`, `2000s`, ...)
- year (`2004`)
- month (`2004-12`)

All buckets retain their exact contributing file IDs for deterministic filtering.
Unplaced photos are never inserted into a dated density bucket.

## Bounded visualisation

The UI keeps only lightweight `TimelineItem` records for the selected chronology
scope. Catalogue rows and thumbnails are materialised in bounded pages of 120
items. The page size is deliberately independent of chronology semantics.

Controls:

- Decades / Years / Months selector
- density counts beside every jump target
- Previous / Next page
- fast scrubber mapped to the current lane's page index
- page/position indicator

Opening Preview from Timeline is bounded to the current visual page rather than
constructing a preview model for every photo in a large year or archive.

## Invariants

1. Scale changes never change TimelineItem placement or authority.
2. Paging never changes item chronology order.
3. Date ranges retain their start/end precision.
4. Unplaced material never appears on a dated scale.
5. Invalid scale, bucket, lane, page, or scrubber state fails closed.
6. The visual grid materialises no more than one bounded page per lane.
7. No source or catalogue writes are performed.
