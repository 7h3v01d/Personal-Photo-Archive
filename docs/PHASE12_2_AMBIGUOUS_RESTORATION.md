# Phase 12.2 — Ambiguous Restoration & File-Origin Preservation

Phase 12.2 closes the oldest remaining Archive Core adversarial backlog item:
when more than one historical byte-identical File can explain a newly observed
path, the scanner no longer selects one candidate by catalogue/insertion order.

## The ambiguity

Consider one logical Photo with two exact physical Files, `A` and `B`. Both are
later missing. A byte-identical file then appears at a new path `C`.

SHA-256 proves that `C` has the same content, but it does **not** prove whether
`C` is:

- `A` moved/restored;
- `B` moved/restored; or
- a newly created third physical copy while both historical Files remain missing.

Choosing `A` or `B` would manufacture physical-File history that PPA did not
observe.

## Phase 12.2 reconciliation rule

For an unrecognised path with a known SHA-256:

1. **Same historical relative path + same hash** remains a deterministic
   restoration of that File.
2. If exactly **one** unmatched same-hash historical File has an absent path,
   the existing confirmed relocation/restoration behaviour is retained.
3. If **multiple** unmatched same-hash historical Files could explain the
   observation, PPA does not choose any of them.

In case 3, the scanner creates a new physical `File` record for the object it
actually observed and leaves every historical candidate unchanged/missing.

If all historical candidates already belong to one logical Photo, the new File
is attached to that Photo because logical-photo identity is not ambiguous. If
the candidate Files span multiple logical Photos, PPA also refuses to choose a
Photo: it creates a fresh logical Photo for the newly observed File and leaves
later human identity-resolution machinery to reconcile it if appropriate.

## Why Phase 12.1 filesystem object identity does not break the tie

Phase 12.1 records `(device id, filesystem object/file-index id)` and link count.
That evidence is excellent for **current-state** hard-link/object accounting.
It is intentionally not treated as a durable historical identity across an
absence.

Filesystem inode/file-index values can be reused after an object is deleted.
Therefore a later matching token cannot, by itself, prove which historical File
returned. Phase 12.2 fails closed rather than promoting that stale token to
historical authority.

## Append-only ambiguity evidence

Migration 028 adds `file_origin_ambiguities`. Each ambiguity observation records:

- the newly observed File;
- Library and scan session;
- full SHA-256;
- observed path;
- canonical JSON snapshot of every candidate File id;
- canonical JSON snapshot of the candidate logical Photo ids;
- whether all candidates were already missing (`ambiguous_restoration`) or the
  candidate set also contained a not-yet-accounted-for present File
  (`ambiguous_relocation`).

The ledger is evidence, not mutable resolution state. No candidate is rewritten
as the winner and no historical observation is deleted.

An `origin_ambiguous` integrity event is also attached to the newly observed
File.

## Archive Health schema v3

Structured Archive Health output is now `ppa-archive-health/3` and adds:

- **Recorded Ambiguous File Origins** — logical Photos containing a currently
  catalogued File that originated from a Phase-12.2 ambiguity observation.

These Photos are included in **Needs Attention**. The browser remains read-only.

## Scan reporting

`ScanReport` now exposes `ambiguous_origin_files`. These observations are counted
as scanned/reconciled Files, but they are deliberately not misreported as
`new_files`, `restored_files`, or `moved_files`.

## Safety boundary

Phase 12.2 changes catalogue reconciliation only. It never writes, renames,
moves, deletes, repairs, or otherwise mutates source photographs.
