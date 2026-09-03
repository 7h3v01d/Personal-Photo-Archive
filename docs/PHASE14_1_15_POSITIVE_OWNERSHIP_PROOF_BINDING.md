# Phase 14.1.15 — Positive Ownership Proof Binding

## Purpose

Phase 14.1.15 completes the positive-operational-authority model introduced in
14.1.14.  Operational ownership is no longer inferred from pathname state
before or after a secure filesystem operation.  Ownership evidence is now
carried by the exact descriptor/handle-bound creation or installation operation
that produced the object.

No schema migration is required. Migration 039 remains the durable ownership
store for `operational_directories` and `operational_files`.

## Security invariant

> Positive ownership must be proven by the exact creation or installation
> operation, not by a caller-controlled boolean and not by a later pathname
> `stat()`.

A path being absent at time A does not prove that an object found at time B was
created by PPA. Likewise, a pathname being PPA-owned at time A does not authorise
replacement of a different object substituted at that pathname at time B.

## 1. Creator-issued directory provenance

`ensure_directory_authority()` now preserves
`final_component_created_by_this_operation` on the bound authority.

The flag is true only when the final directory component was produced by the
exclusive handle/descriptor-relative creator in that same operation. Binding an
already-existing directory always yields false provenance.

`require_directory()` no longer accepts `created_by_ppa: bool`. An unenrolled
operational directory can be implicitly enrolled only when the returned bound
authority itself carries creator-issued provenance.

Explicit trusted enrollment remains a separate API through
`enroll_existing_directory()` / `enroll_export_root()`.

This closes the bootstrap race for:

- thumbnail cache roots;
- export roots;
- recovery-preservation roots.

If a post-scan source directory is moved onto a previously absent operational
pathname before authority acquisition, PPA binds that exact object but refuses
to enroll it because the secure creator did not create it.

## 2. Exact destination identity during replacement

`BoundTemporaryFile.install()` accepts an optional
`expected_existing_identity`.

When replacement is authorised, the installer verifies that the destination
object it is about to park/replace is the exact positively-owned filesystem
identity supplied by the caller.

Windows performs this check against the native handle opened for rename before
any namespace mutation.

POSIX verifies the destination before parking and verifies the parked object
again before proceeding or deleting anything. If a substitution wins the
stat-to-rename interval, rollback restores the displaced object and installation
fails.

A newly appearing destination is never replaced under "new file" semantics;
creation uses `replace=False` and therefore fails closed.

## 3. Installer-derived file ownership recording

Security-sensitive writers no longer call `stat()` on the destination pathname
after installation to learn what object to trust.

`record_owned_file_identity()` persists the identity already held by the
installed `BoundTemporaryFile` (`temp.identity`) together with the intended
canonical ownership key captured before installation.

Consequently, a source object substituted after installation but before the DB
checkpoint cannot be blessed as a PPA export.

## 4. Positive ownership for thumbnail children

An enrolled thumbnail cache directory is not blanket authority to replace every
correctly-shaped child name.

Generated thumbnail PNGs and attestation JSON files are now recorded in
`operational_files` under purpose `thumbnail_cache_child` when catalogue-backed
ownership is available.

Replacement is allowed only when the existing child still has the exact
positively-owned identity recorded for it. An unowned child appearing at the
expected cache key is left untouched and generation fails closed.

Without a catalogue authority database, existing cache children remain readable
legacy/cache state but are never implicit replacement authority.

Cleanup is deliberately conservative: non-essential cache debris is preferable
to widening deletion authority over a raced pathname.

## Permanent adversarial regressions

Phase 14.1.15 adds regressions for the attack interleavings identified during the
14.1.14 adversarial review:

1. post-scan source directory inserted after apparent absence, before thumbnail
   cache authority acquisition;
2. the same bootstrap forgery against a safe-export root;
3. the same bootstrap forgery against the recovery-preservation root;
4. positively-owned export checked, then a source file substituted immediately
   before installation;
5. export installed, then pathname substituted immediately before ownership
   recording;
6. unowned source child moved into a legitimately enrolled thumbnail cache under
   the exact expected cache filename.

Expected result in every destructive case is fail-closed with source bytes
unchanged and no accidental ownership enrollment.

## Compatibility

- Migration/schema version remains 39.
- Explicit manual enrollment remains available as an intentional trust boundary.
- Existing 14.1.14 operational-directory rows remain valid.
- Existing legacy thumbnails may still be read; they are not automatically
  promoted to replacement authority.
- Existing export files already recorded in `operational_files` remain
  replaceable when their exact identity still matches.

## Verification gates

The Phase 14.1.15 implementation is expected to pass:

- focused safe-export / thumbnail / preservation tests;
- high-risk hardening and Windows-reparse regression sets;
- complete repository test coverage;
- `compileall`;
- wheel packaging / fresh-database migration checks.
