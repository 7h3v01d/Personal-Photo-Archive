# Phase 9.12 — Organisation Export / Shareable Curation Report

Phase 9.12 adds a sanitized, read-only export of the human organisation layer.
The ZIP contains only `organization-report.json`, `organization-report.md`, and
`README.txt`.

Included: Album names/descriptions/counts, Tag names/counts, organisation-health
counts, saved discovery view names/selector names, assisted-suggestion review
status/notes, and recent human-readable organisation activity.

Excluded by design: source photographs, thumbnails, filesystem paths, database
paths/files, Photo/File/Album/Tag UUIDs, hashes, raw SQL rows, chronology evidence,
anchors, reconstructions, and archive internals.

Export runs on a background SQLite connection in the desktop UI and is a
read-only archive projection. The output ZIP is written atomically via a sibling
temporary file and `os.replace`.
