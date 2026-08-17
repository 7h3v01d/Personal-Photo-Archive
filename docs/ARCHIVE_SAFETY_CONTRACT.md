# Archival Safety Contract

This is the contract every phase of Personal Photo Archive is checked
against. If a feature requires violating one of these rules, the rule wins
and the feature gets redesigned.

## Permitted operations on source files

- Read file bytes (for hashing, thumbnailing, opening in an editor view)
- Read embedded metadata (EXIF/IPTC/XMP)
- Read filesystem metadata (path, size, mtime, ctime)

## Forbidden operations on source files (until "managed library" mode exists)

- Writing to the file
- Deleting the file
- Moving or renaming the file
- Modifying the file's mtime/ctime
- Rewriting embedded metadata in place

No exceptions, including "just to fix an obviously wrong date" — corrections
live in the catalogue, never in the source file.

## Rules

1. Source files are read-only by default.
2. No automatic deletion, ever, of anything — not even confirmed exact
   duplicates. Deletion is always an explicit, reviewed user action.
3. No automatic metadata rewriting. Interpreted/corrected values are stored
   as catalogue rows referencing the file, not written back into it.
4. Every imported photograph receives a persistent ID at first sight, which
   never changes and is never reused.
5. Every interpretation records where it came from (source, evidence,
   confidence) — nothing is stored as a bare, unattributed fact.
6. All destructive operations (in the sense of "removes information the
   user can't get back," e.g. deleting a database row, discarding a
   duplicate) require explicit, in-the-moment user action. Batch operations
   must show what will happen before it happens.
7. Database schema changes must be migratable — no dropping/rebuilding the
   catalogue to add a column.
8. Analysis must be reproducible where practical: given the same inputs and
   the same rule version, re-running an analysis should produce the same
   output, and the log should make it clear what ran and when.

## What "managed library" mode changes

When Phase 23 (Managed Archive) is implemented, an explicit opt-in import
step may copy files into an archive-controlled structure. At that point the
*archive's own copies* become subject to different rules (the archive may
reorganise its own managed files). The user's original source location is
never touched by this process — it is only ever read from, once, to make
the copy.

---

*This document is a living Phase 0 deliverable. Revise it deliberately and
explicitly — don't let a rule get quietly relaxed by a feature branch.*
