# Phase 8.6 — Event Story Context

Phase 8.6 adds richer **human-authored memory** to durable Events without allowing narrative text to become chronology evidence.

## Context fields

- short description
- remembered place
- people / relationship notes
- occasion / context
- longer story / memory

The pre-existing Event `note` remains a short curation note. Story Context is stored separately in `event_context` and each real change appends a full before/after snapshot to `event_context_history`.

## Authority boundary

Story Context is interpretation only. It is never read by Phase 6 dating, Phase 7 reconstruction, Phase 8 timeline placement, or event clustering. A sentence such as "Definitely Christmas Day 1999" cannot move an unplaced photograph onto the timeline.

No source photographs, EXIF, metadata observations, anchors, reconstructions, or confirmation states are modified.
