# Phase 14.1.17.4 — Evidence Link-Topology Finalization

## Purpose

Phase 14.1.17.4 closes a late hard-link race in Phase-14 evidence finalisation. Earlier revisions correctly verified that operational evidence began as a single-link regular file, but a second hard link could be created after that check and before the immutable recovery checkpoint. A post-commit `chmod 0444` would then mutate the shared inode and therefore change metadata visible through a source-Library alias.

## Invariant

A Phase-14 evidence checkpoint may accept a filesystem file only when the exact object is freshly re-attested immediately before commit as:

- a regular non-reparse file;
- the exact expected filesystem identity;
- `st_nlink == 1`;
- the expected byte size when known;
- the expected SHA-256, hashed through the opened descriptor; and
- still the same single-link object after descriptor-bound hashing completes.

This final gate applies to:

- Phase-14.0 `suspect-source` preservation evidence;
- Phase-14.0 preservation manifests;
- normal Phase-14.1 donor materializations;
- normal Phase-14.1 donor manifests;
- orphan-adopted `expected-donor.*` evidence; and
- an existing filesystem donor-materialization manifest accepted during orphan adoption.

If link topology changes, the operation fails closed. No checkpoint or success event is committed. Existing recovery cleanup rules remain non-destructive and may retain operational debris when exact deletion authority is unavailable.

## No post-commit metadata mutation

Phase 14.1.17.4 removes the advisory post-commit evidence `chmod 0444` step. Filesystem permission bits were never the recovery authority and an owning user could reverse them. More importantly, chmod mutates the inode shared by every hard-link alias.

The final sequence is therefore:

1. final exact-object identity/content/single-link attestation;
2. append immutable catalogue evidence and integrity event;
3. commit;
4. return without any further evidence filesystem mutation.

The catalogue checkpoint and hashes are the authority.

## Permanent adversarial regressions

The suite includes late-hardlink attacks for:

1. orphan donor adoption after its initial single-link/source-authority checks;
2. normal Phase-14.1 donor materialization after evidence creation but before checkpoint; and
3. Phase-14.0 preservation after staging but before checkpoint.

Each regression requires the operation to fail before checkpoint, preserve the source-Library alias bytes and mode, and emit no false success/adoption event.

## Schema

No migration is required. Catalogue schema remains **v39**.
