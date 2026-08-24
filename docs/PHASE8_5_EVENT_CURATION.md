# Phase 8.5 — Event Curation & Membership Management

Phase 8.5 makes durable human Events editable without weakening chronology truth.

## Authority boundary

An Event membership claim means *this photo belongs to this human-named occasion/context*.
It does **not** mean the photo's capture date is confirmed. Adding or removing a member never
changes Timeline placement, reliability, reconstruction, metadata, EXIF, or source bytes.

## Supported human actions

- rename an Event;
- edit its human note;
- add a catalogued photo from the same Library;
- remove an Event member while retaining at least one member;
- inspect recent curation history.

Every mutating action is explicit and append-audited in `event_history`. Initial Event creation
stores a canonical snapshot of the original authoritative cluster seed membership. Later member
changes never rewrite that historical creation record.

## Membership roles

- `authoritative_seed`: member captured from the authoritative seed of the provisional cluster
  when the Event was created;
- `human_added`: photo explicitly added later by a human.

A seed member may later be removed, but its original role remains recoverable from the history.

## Scope safety

Event membership remains Library-scoped. Python validation and the existing SQLite trigger both
reject cross-Library membership.

## Date-span semantics

The Event's original date span is not silently expanded when a photo is added. Event membership
is semantic interpretation; chronology remains an independent evidence-backed claim. A future
explicit Event date-edit feature, if added, must be separately audited rather than inferred from
membership.

## Schema

Migration 014 adds the append-only `event_history` table. No photographic evidence tables are
changed.
