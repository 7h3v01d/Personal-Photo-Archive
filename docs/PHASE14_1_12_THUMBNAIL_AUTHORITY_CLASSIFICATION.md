# Phase 14.1.12 — Thumbnail Authority Classification

## Trigger

The Phase-14.1.11 adversarial review accepted the new authority-bootstrap ordering but reproduced a thumbnail-specific source-write bypass. `ThumbnailCache` bound the exact filesystem object correctly, yet classified that object as safe primarily from directory contents. An empty registered Library could therefore receive the cache marker, and a catalogued source PNG whose filename matched the cache naming convention could be overwritten by a generated derivative after the Library object was moved onto the cache pathname.

## Invariant

**Directory contents are not authority identity.**

Writable thumbnail authority is now established as:

```text
explicit catalogue-backed Library exclusion policy
        ↓
bind exact existing ancestor/directory object
        ↓
validate THAT object against registered Library paths + filesystem identities
        ↓
create any missing child relative to the validated authority
        ↓
validate the child before it can create another child
        ↓
only then inspect marker/cache shape for operational hygiene
        ↓
marker / derivative / attestation write
```

The cache marker and cache-shaped filenames are never security credentials.

## Implementation

- `ThumbnailAuthorityPolicy.from_connection(conn)` snapshots registered `root_canonical_path`, `root_fs_device_id`, and `root_fs_object_id`.
- Any registered Library without verified root filesystem identity blocks writable thumbnail-cache bootstrap until the Library is rescanned.
- `ThumbnailCache` requires either `conn=` or an explicit `authority_policy=`. Omitting both fails before any cache directory is created.
- `ensure_directory_authority(cache_dir, validator=policy.validate_authority)` applies Library exclusion to the nearest existing ancestor and to every newly created directory component.
- Exact object-identity match with a registered Library fails closed even when that Library has been moved onto a previously safe cache pathname.
- Current path containment inside a registered Library also fails before child creation.
- `ThumbnailWorker` receives catalogue context explicitly and snapshots the policy before it moves to its worker thread.
- Forensic current/expected caches in `mismatch_investigation.py` use the same catalogue-backed policy.
- No change is made to `secure_write`, Phase-14 evidence semantics, target-write authority, or schema v37.

## Permanent regressions

1. Writable cache construction without Library authority context fails before creating a cache directory.
2. A missing cache path beneath a registered Library fails before creating its first child.
3. An empty registered Library moved onto the cache pathname receives no `.ppa-thumbnail-cache-v1` marker.
4. A registered Library containing a catalogued `<sha256>-64.png` source photo can be moved onto the cache pathname without that source photo being rewritten; bootstrap fails before derivative creation.
5. Existing post-bootstrap real-directory substitution, hard-link temp substitution, attestation integrity, safe-export and Phase-14 bootstrap regressions remain green.

## Release boundary

Phase 14.1.12 remains a recovery/source-write hardening slice only. It introduces no source-photo write authority and no recovery-target replacement authority.
