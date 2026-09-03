# Phase 14.1.5 — Windows Adoption Write-Authority Hardening

## Why this patch exists

Phase 14.1.4 fixed the Windows orphan-liveness dead end by allowing a fully
verified final donor orphan to move forward without unsafe pathname deletion.
A subsequent adversarial review found one remaining authority gap: when the
filesystem donor manifest was missing, the Windows adoption path still created
`donor-materialization.json` through the validated stage pathname.  Because
Windows has no `BoundDirectory` write authority in this implementation, an
ordinary directory could replace the validated stage between the final check and
that write, redirecting recovery bookkeeping into a registered source Library.

Phase 14.1.5 removes that write entirely.

## Authority rule

On a platform without descriptor-bound stage-directory mutation, **verified
orphan adoption may not create new filesystem evidence through the stage
pathname**.

When the final `expected-donor.<ext>` artifact is fully verified but no valid
filesystem donor manifest exists, PPA:

1. freshly rebuilds and revalidates all Phase-13/14 authority;
2. freshly verifies the orphan donor, original source donor and target state;
3. constructs the canonical `ppa-recovery-donor-manifest/1` payload in memory;
4. hashes the exact canonical UTF-8 bytes;
5. rechecks stage/orphan/source/target evidence;
6. stores the canonical manifest payload and hash in the append-only catalogue;
7. appends the immutable Phase-14.1 materialisation checkpoint;
8. creates **no** `donor-materialization.json` filesystem file.

The checkpoint records:

- `donor_manifest_storage = 'catalogue_embedded'`;
- the intended manifest pathname for provenance/context;
- `donor_manifest_payload_json` containing the canonical manifest bytes as text;
- `donor_manifest_sha256` over those exact canonical bytes.

Normal donor materialisation and orphan adoption where a valid manifest already
exists continue to use:

- `donor_manifest_storage = 'filesystem_file'`;
- a descriptor-bound, independently hashed filesystem manifest;
- `donor_manifest_payload_json = NULL`.

## Schema

Migration 036 adds the manifest-storage discriminator and optional embedded
payload to `archive_recovery_donor_materializations`.  The existing append-only
UPDATE/DELETE protections continue to apply to the whole row.

## Real-directory substitution regression

The permanent regression reproduces the adversarial sequence without any
symlink, junction or reparse point:

```text
validated recovery stage
        ↓
ordinary source Library contains donor-materialization.json
        ↓
stage renamed away
        ↓
source Library renamed into validated stage pathname
        ↓
Windows-style orphan adoption reaches old manifest-write boundary
```

Expected result:

- adoption aborts because stage identity changed;
- the user-owned source-Library JSON file is byte-for-byte unchanged;
- no recovery checkpoint is committed;
- source donor and target are unchanged.

Because the missing-manifest adoption path has no filesystem write at this
boundary, pathname substitution cannot redirect bookkeeping bytes into user data.

## Non-authority statement

Phase 14.1.5 still does not authorise target replacement, donor-to-target copying,
source rename/move/delete, EXIF writeback or timestamp repair.  It changes only
how a Windows/unsupported-platform orphan manifest is durably represented.
Schema advances to **v36**.
