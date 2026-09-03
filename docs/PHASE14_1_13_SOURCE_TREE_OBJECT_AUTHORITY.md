# Phase 14.1.13 — Source-Tree Object Authority

## Status

Active freeze candidate. This is a narrow authority-classification correction over Phase 14.1.12. It does not alter `BoundTemporaryFile`, `BoundDirectory`, Windows NT handle-relative file/directory mutation, Phase-14 recovery evidence semantics, or target-write authority.

## Adversarial finding

Phase 14.1.12 correctly rejected a registered **Library root** that had been renamed onto a thumbnail-cache pathname. The next adversarial pass showed that a child directory previously observed inside that Library could be renamed outside the Library tree and then evade both root-object equality and current-path containment. A cache-shaped catalogued PNG inside that moved child was overwritten by a generated thumbnail.

The missing invariant was therefore not filesystem binding; it was source-tree classification:

> A directory object that PPA has observed as part of a registered source Library remains source-associated by filesystem identity even if its pathname later moves outside the Library root.

## Migration 038

Migration `038_source_tree_object_authority.sql` adds:

- `libraries.source_tree_identity_complete`
- `libraries.source_tree_identity_verified_at`
- `library_directory_identities`

Each directory row records:

- owning `library_id`;
- canonical path at latest observation;
- filesystem device identity;
- filesystem object identity;
- first observation time;
- latest verification time.

The `(library_id, device, object)` tuple is unique and its object identity cannot be rebound in place.

Directory rows are historical source-authority evidence. A directory disappearing from its old pathname does **not** remove the row. Forgetting the owning Library is the explicit retirement boundary through `ON DELETE CASCADE`.

## Complete-scan authority

A source-tree identity set is security authority, so partial knowledge is never treated as permission.

Before traversal begins, the scanner marks the Library source-tree inventory incomplete. During traversal it records every real directory actually visited, including empty directories. Only a complete scan marks the inventory complete again.

Consequently, after upgrading to schema v38, writable operational output fails closed until every registered Library has completed a current scan. An incomplete or crashed scan likewise withdraws the completeness claim until a later successful scan.

## Shared source-tree policy

`ppa.source_tree_authority.SourceTreeAuthorityPolicy` is the shared classification layer. It snapshots:

- every verified registered Library root pathname;
- every historically observed Library directory filesystem identity.

For an already-bound candidate directory, policy evaluation is:

```text
bind exact directory object
        ↓
compare exact (device, object) identity
against historical source-tree identities
        ↓
MATCH → reject
        ↓
check current registered-root topology
        ↓
inside source tree → reject
        ↓
only then grant operational authority
```

The same policy is now consumed by:

- thumbnail cache authority;
- safe-export parent authority;
- Phase-14 preservation-root authority.

The secure-write primitives remain unchanged.

## Permanent regressions

Phase 14.1.13 adds attacks covering:

1. a populated Library child containing a cache-shaped catalogued PNG moved onto the thumbnail-cache pathname — cache bootstrap fails before marker/PNG/attestation mutation and the source SHA remains unchanged;
2. an empty observed Library child moved onto the thumbnail-cache pathname — rejected independent of contents;
3. an observed Library child moved onto a safe-export parent pathname — export fails and source sidecar bytes/entries remain unchanged;
4. an observed Library child moved onto the Phase-14 preservation-root pathname — staging fails before any UUID stage is created in that source object;
5. source-directory identities persist after a directory moves away from its original Library pathname;
6. an upgraded/unscanned source-tree inventory remains incomplete and cannot authorize writable operational output.

## Release boundary

Phase 14.1.13 still introduces no source-photo write authority and no recovery-target replacement authority. It expands only the set of filesystem directory objects that PPA recognizes as protected source authority.
