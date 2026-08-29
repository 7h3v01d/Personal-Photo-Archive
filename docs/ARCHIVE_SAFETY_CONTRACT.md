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
9. A verified content mismatch is never silently adopted as new archive truth.
   Changing current FileRevision authority requires an explicit, reviewed human
   decision bound to the exact bytes/revision shown, followed by fresh
   revalidation and append-only audit history. The source file remains read-only.
10. Expected revision identity is not proof of current physical bytes. Logical
    Photo identity-changing operations may use positive byte-identity evidence
    only from a present, healthy, coherent current FileRevision whose SHA is
    verified-current. Missing, unreadable, mismatched or incoherent current
    content is UNKNOWN for merge/split/recovery/exact-copy authority.
11. Catalogue verified-current identity is necessary but not sufficient at an
    identity-changing execution boundary. Merge, split and recovery must freshly
    re-attest every relevant physical source File against the reviewed SHA
    immediately before mutation and again before commit; exact-copy action
    validation must likewise re-attest the selected Files. External change,
    disappearance, unreadability or observation instability fails closed.
12. A recovery *plan* is not recovery authority. Phase-13.0 donor qualification
    and proposal recording may read/re-attest source bytes and append catalogue
    evidence, but must carry `execution_authorized=false` and must not copy,
    replace, rename, move, delete or repair a source photograph. Filesystem
    device/object IDs may rank topology evidence but never prove independent
    physical backup hardware or failure domains.
13. User-directed exports and durable application artifacts must never use a
    registered source Library as an output destination. Export writers must fail
    closed on source-path/symlink/hard-link aliases and protected PPA operational
    state, and must stage output atomically rather than opening an existing
    destination inode for writing.
14. Recovery preservation is not recovery replacement authority. Phase 14.0 may
    write an exact copy of the currently suspect target bytes only to dedicated
    PPA operational preservation storage outside every registered source Library,
    after a frozen Phase-13 proposal and fresh target/donor physical evidence are
    revalidated. The preservation copy must be independently re-hashed and the
    physical source/donor re-attested again before commit. Phase 14.0 must not
    materialize donor bytes, replace/create the target, or mutate any source File.

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


### Phase 14 operational-stage cleanup invariant

Recovery preservation stage IDs are canonical UUIDs and may never carry path authority. Rollback cleanup must be identity-bound to the exact operational directory PPA created and may remove only PPA-owned stage artifacts; it must not recursively traverse or chmod unexpected filesystem aliases.

### Phase 14.1 donor-materialization invariant

Verified donor materialization is operational recovery evidence, not target-replacement authority. It may copy a freshly re-attested donor into the already committed protected recovery stage only after the Phase-13 proposal, Phase-14 preservation evidence, current human recovery intent, target state, and donor SHA are revalidated. The original donor and target remain read-only, and a Phase-14.1 checkpoint must carry `target_replacement_performed=0` and `recovery_execution_authorized=0`.
