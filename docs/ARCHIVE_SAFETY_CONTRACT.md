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

### Descriptor-bound temporary-write invariant (Phase 14.1.1)

Any PPA path that stages bytes in a temporary file before installation must keep write authority bound to the exact filesystem object created by the exclusive temporary-file operation. Code must not close that descriptor and later reopen the temporary pathname for writing. Before installation, the temporary pathname and open descriptor identities must still agree; pathname substitution fails closed. This applies to recovery staging, donor materialization, exports, reports and thumbnail/cache derivatives.

### Recovery-checkpoint deletion invariant (Phase 14.1.1)

Phase-13 recovery proposals and Phase-14 preservation/materialization checkpoints are append-only forensic evidence. They may not be UPDATEd or DELETEd, including through foreign-key cascade. Any future retirement/purge semantics require a separately reviewed explicit design rather than implicit cascade deletion.
### Recovery commit-boundary invariant (Phase 14.1.2)

Rollback cleanup owns Phase-14 operational artifacts only while the corresponding immutable catalogue checkpoint is still rollback-able. Once a preservation or donor-materialization checkpoint is durably committed, those files are authoritative evidence and exception cleanup must never delete them. If commit status is ambiguous after SQLite has left the transaction, cleanup fails closed toward preserving evidence. Donor-orphan reconciliation is a writer-authority operation and must serialize with donor materialization under `BEGIN IMMEDIATE`, with checkpoint authority rechecked immediately before any unlink.

Directory identity checks are not destructive authority. Any Phase-14 cleanup or reconciliation that removes operational children must remain bound to the exact open directory object that was authorised. On platforms supporting descriptor-relative operations, child stat/unlink/rename must be performed relative to that directory handle using validated child names. A later rename, symlink, junction or mount substitution of the directory pathname must not redirect mutation. If equivalent handle-bound deletion cannot be guaranteed on a platform, automatic cleanup must fail closed and leave operational debris.

### Windows orphan-forward and reparse-point invariant (Phase 14.1.4)

Windows is not granted pathname-based destructive cleanup merely to recover liveness. If descriptor-bound directory deletion is unavailable, PPA may move an interrupted Phase-14.1 stage forward only by **verified orphan adoption**: the final donor artifact must be a single-link regular non-reparse file reproducing the immutable expected SHA/size; the committed stage, source donor, target state and human recovery intent must all be freshly revalidated; ambiguous pending/temp debris fails closed to manual intervention. Windows reparse points, including junctions, are never valid recovery/output/cache authority, and existing reparse components in a protected operational path are rejected.

### Windows orphan-adoption write-authority invariant (Phase 14.1.5)

When descriptor-bound stage-directory write authority is unavailable, verified orphan adoption must not create a missing recovery manifest through the stage pathname. A missing manifest is reconstructed canonically in memory and embedded in the append-only catalogue with its SHA-256; no filesystem manifest write is permitted. A pre-existing valid filesystem manifest may be verified and referenced read-only. Ordinary-directory pathname substitution must therefore have no route to create, replace, rename or delete user data in a registered source Library during orphan adoption.


### Parent write-authority invariant (Phases 14.1.7–14.1.10)

A directory pathname that was validated earlier is not sufficient filesystem write authority. When higher-level logic has authorised a specific output/stage/cache directory object, every later temporary-file creation and installation must carry and prove that same parent filesystem identity. On POSIX, child creation/install remains descriptor-relative to the authorised `BoundDirectory`. On Windows, Phases 14.1.8–14.1.10 require native handle-relative child creation and namespace mutation against the exact authorised directory object; Phase 14.1.9 uses `NtCreateFile` plus `NtSetInformationFile(FileRenameInformation)` with the same directory handle; preventing rename through share-mode assumptions is not sufficient. The lexical pathname may be revalidated for liveness/reporting-success decisions, but it is never the authority that selects where bytes or directory entries are created, renamed, replaced, or removed. A writer must never silently establish new authority over whichever ordinary real directory later occupies a previously validated pathname. This invariant applies to Phase-14 preservation/materialisation, manifests, user-directed safe exports, and thumbnail/cache derivatives.

## Authority-bootstrap invariant (Phase 14.1.11)

A safe pathname is not write authority. PPA must select/bind the exact filesystem directory object first, prove **that same object** is outside registered source-Library authority and protected operational boundaries, and only then permit it to create children. The same object identity/handle must flow into all subsequent namespace mutation.

Library root pathname and Library root identity are distinct facts. A verified Library root's filesystem `(device, object)` identity is persistent evidence; a pathname that later names another object does not silently redefine the Library. Unverified/NULL Library root object identity is UNKNOWN and cannot authorize archive-sensitive output/recovery writes.


## Thumbnail authority classification

A thumbnail/cache directory receives write authority only from an explicit catalogue-backed exclusion policy, never from its contents. `.ppa-thumbnail-cache-v1`, cache-shaped PNG names, attestation names, emptiness, or any other directory-content heuristic are operational metadata and are **not security credentials**. Before any cache directory component, marker, derivative, or attestation is created, the exact bound directory authority must be checked against verified registered Library-root filesystem identities and current registered Library-root topology. Missing cache path components must be created only through `ensure_directory_authority(..., validator=...)`, so a registered Library or a path inside one cannot become a parent for cache creation before classification. Omitting Library authority context fails closed.

## Source-tree object authority invariant (Phase 14.1.13)

Registered source authority is the historically observed Library **directory tree**, not only the current root pathname or root filesystem object. A complete Library scan records every traversed directory object's `(device, object)` identity, including empty directories. Once observed, that identity remains source-associated even if the directory is later renamed outside the Library pathname; disappearance does not silently retire source authority. Forgetting the owning Library is the explicit retirement boundary.

Writable operational roots—including thumbnail caches, safe-export parents and Phase-14 preservation roots—must bind the exact candidate object first and reject it when its identity matches **any** historically observed Library directory identity. Current pathname containment remains a secondary topology check, never the sole source-tree classifier. Cache markers, cache-shaped filenames, emptiness, and other contents are not authority credentials. A source-tree inventory that has not completed a successful scan is UNKNOWN and must fail closed for authority-sensitive writes.



## Positive operational authority invariant (Phase 14.1.14)

PPA-owned writable locations are trusted because PPA positively owns the exact filesystem objects enrolled for their operational purpose, not because those objects are merely absent from source-history deny lists. Historical source-tree identity remains mandatory defense in depth but is not primary proof that an arbitrary directory is safe.

Thumbnail-cache and Phase-14 preservation roots must either be newly created by PPA through bound directory authority and immediately enrolled, or exactly match a previously enrolled operational-directory identity. A pre-existing un-enrolled directory is never silently adopted merely because it occupies the expected pathname. If an enrolled operational object is moved or replaced, the old pathname does not confer authority on the replacement object.

Catalogue-backed user exports require an enrolled export-root object. Existing destination files are replaceable only when the exact destination filesystem object is already recorded as a PPA-owned export; arbitrary pre-existing or newly appeared files fail closed. Export-root enrollment is an explicit trust action, never an implicit consequence of calling the writer. Utility/report output with no catalogue and no configured source-Library context may retain descriptor-bound atomic output because no archive authority database exists in that mode.
