# Phase 8.11 — Event Search & Discovery

## Purpose

Make durable human Events discoverable without introducing another interpretation or chronology path.
Search is a read-only index over Event identity and human-authored narrative context.

## Searchable fields

- Event name
- Occasion/context
- Remembered place
- People notes
- Description
- Story text
- Short curation note

Search is case-insensitive and whitespace-normalised. Multiple query tokens use AND semantics: every token must match at least one searchable field.

## Explainable ranking

For non-empty queries, each token contributes the highest matching field weight:

1. Event name
2. Occasion
3. Place
4. People
5. Description
6. Story
7. Note

Ties fall back to the existing deterministic Family History position. Empty search preserves Family History chronological order exactly.

## Filters

- Start year (Event start year)
- Inclusive From date
- Inclusive To date

Date filtering uses overlap with the durable Event span. It does not inspect or reinterpret individual member-photo chronology.

## Authority boundary

Search matches are discovery metadata only. Narrative text cannot:

- place or move a photo on Timeline;
- change Phase-6 reliability;
- change Phase-7 reconstruction/confidence;
- create an anchor;
- alter Event membership;
- alter Event date span;
- write EXIF, metadata observations, or source bytes.

## Desktop

Family History now includes a search box plus From/To date filters. Results are filtered/ranked in memory after the background Family History/search-index build, so typing does not rerun chronology or issue database searches per keystroke. Existing year navigation and 30-card paging remain in force.

## CLI

```text
python -m ppa.cli event-search <library_id> "sydney maddie"
python -m ppa.cli event-search <library_id> "christmas" --year 2004
python -m ppa.cli event-search <library_id> "family" --from 2004-01-01 --to 2006-12-31
python -m ppa.cli event-search <library_id> "mum presents" --json results.json
```

Schema: `ppa-event-search/1`.

## Persistence

No schema migration. Phase 8.11 is a read-only projection/index over existing durable Event and Story Context state.
