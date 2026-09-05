# Phase 14.0.1 — Stage-Path & Rollback-Cleanup Hardening

Phase 14.0 introduced the first recovery write boundary: preserving suspect source bytes into PPA operational storage while leaving all source photographs untouched. The follow-up hardening closes a filesystem-authority flaw in the operational stage identifier and failure cleanup.

## Closed defect

`stage_id` is used as a filesystem path component. Accepting arbitrary caller text would allow values such as `../library` or an absolute path to escape `recovery-preservation/`. A subsequent failure could then cause generic recursive cleanup to act on the escaped path.

Phase 14.0.1 therefore requires every stage identifier to be one canonical UUID. The invariant is enforced both when a plan is built and again at execution, so a forged plan object cannot bypass it. Invalid identifiers are rejected before PPA creates the preservation root or stage directory.

## Identity-bound cleanup

After the stage directory is created, PPA records its filesystem device/object identity. Rollback cleanup proceeds only if that exact directory object still exists directly beneath the validated preservation root. Cleanup unlinks only artifacts PPA itself created. It does not use recursive tree deletion, does not recurse into unexpected child directories, and does not chmod unexpected symlinks. Unexpected content therefore causes a diagnostic remnant to be left behind rather than expanding cleanup authority.

## Operational-output protection

Successful custom preservation roots recorded in `archive_recovery_preservation_stages` are treated as protected PPA operational trees by `ppa.safe_export`, just like the default database-adjacent `recovery-preservation` root. Ordinary exports therefore cannot overwrite preservation evidence stored at a custom location.

## Durability and permission hardening

The parent preservation root is fsynced after creation of the stage-directory entry on platforms where directory fsync is available. Preservation and manifest files remain byte-hashed before the catalogue checkpoint. Superseded by Phase 14.1.17.4: evidence is re-attested for exact identity, content, and single-link topology immediately before commit, and no post-commit chmod is performed.

## Authority boundary

This patch does not increase recovery authority. Phase 14.0.1 may copy the current suspect target bytes into protected operational preservation storage after revalidation. It still does not materialise donor bytes or write, replace, rename, move, delete, metadata-rewrite, timestamp-repair, or otherwise alter any source photograph.
