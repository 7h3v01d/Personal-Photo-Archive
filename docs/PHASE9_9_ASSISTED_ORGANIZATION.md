# Phase 9.9 — Assisted Organisation Suggestions

Phase 9.9 adds a conservative review layer over explicit human curation.
It does not use image similarity, face recognition, filenames, dates, EXIF,
Timeline placement, or Story prose to invent organisation.

## Current suggestion rule

A Tag-gap suggestion may be emitted for an existing Album or named Event when:

- the group contains at least 5 distinct logical Photos;
- at least 4 of those logical Photos already carry the same existing Tag;
- Tag coverage is at least 80%; and
- at least one group Photo remains without that Tag.

The suggestion identifies the existing Tag and the explicit peer group, and
lists only the missing logical Photo IDs as review candidates.

Event membership is deduplicated from File identity back to logical Photo
identity before support is calculated, so duplicate physical copies cannot
inflate evidence for a suggestion.

## Human approval and stale-state protection

Suggestions are read-only until the user explicitly chooses **Apply suggested
Tag…**. Before any write, PPA rebuilds the current suggestion projection and
requires an exact stable suggestion ID and target set match. If Album/Event/Tag
curation changed after review, the suggestion is stale and is refused.

Accepted suggestions are applied through the existing audited Tag-membership
API. No special privileged write path exists.

## Authority boundary

Suggestions are curation convenience, not photographic evidence. Phase 9.9:

- does not create Tag names;
- does not infer Album/Event membership;
- does not read or write chronology evidence;
- does not change metadata observations, anchors, or reconstructions;
- does not write EXIF or source-photo bytes.

Schema: `ppa-organization-suggestions/1` (projection only; no DB migration).
