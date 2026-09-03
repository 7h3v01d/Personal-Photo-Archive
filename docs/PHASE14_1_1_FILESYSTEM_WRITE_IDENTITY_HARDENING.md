# Phase 14.1.1 — Filesystem Write-Identity Hardening

Phase 14.1's recovery/evidence model remains intact, but an adversarial review found a lower-level source-safety defect shared by several output paths: code could create a secure temporary file, close its descriptor, and later reopen the *pathname* for writing. An external hard-link/symlink substitution between those steps could redirect bytes into a catalogued source photograph even though later verification detected the change and rolled the database back.

Phase 14.1.1 closes that class of defect structurally.

## Descriptor-bound write authority

`ppa.secure_write.BoundTemporaryFile` is now the common temporary-write primitive.

The lifecycle is:

```text
mkstemp()
   ↓
keep control descriptor open
   ↓
capture descriptor + parent directory filesystem identity
   ↓
write only through duplicate handles to that descriptor
   ↓
fsync descriptor
   ↓
compare fstat(descriptor) with lstat(temp pathname)
   ↓
reject pathname substitution
   ↓
close control descriptor where required by platform
   ↓
atomic sibling install
   ↓
verify installed destination is the same filesystem object
   ↓
fsync parent directory
```

A substituted temporary pathname can therefore never receive the write. The write remains attached to the file object created by `mkstemp()`.

This primitive is used by:

- archive-safe JSON/CSV/ZIP/report exports;
- Phase-14.0 suspect-byte preservation;
- Phase-14.1 verified donor materialization;
- Phase-14 preservation/materialization manifests;
- thumbnail generation and thumbnail attestations.

Predictable thumbnail `.tmp` files are no longer used.

## Failure and interruption semantics

Phase-14.0 and Phase-14.1 execution cleanup now runs for `BaseException`, so `KeyboardInterrupt` / `SystemExit` receive the same source-safe rollback cleanup as ordinary exceptions.

A true process crash or power loss cannot execute Python cleanup. Phase 14.1 therefore also has an explicit orphan-reconciliation path:

```text
python -m ppa.cli recovery-reconcile-donor-orphans <stage-id>
```

The reconciler operates only inside an identity-validated committed Phase-14 stage. It never traverses directories or opens orphan artifacts for writing. It removes only the exact uncheckpointed Phase-14.1 final names and recognised random pending names by unlinking their directory entries. Symlink/hard-link aliases are not followed, so source bytes cannot be modified by reconciliation.

## Append-only recovery checkpoints

Migration 035 adds `BEFORE DELETE` protection to:

- `archive_recovery_plan_proposals`;
- `archive_recovery_preservation_stages`;
- `archive_recovery_donor_materializations`.

The earlier UPDATE immutability remains. Parent `ON DELETE CASCADE` paths can no longer silently erase these checkpoints; a future evidence-retirement policy must be explicit rather than inheriting cascade semantics.

## Wheel/package bootstrap

The database migration SQL directory is now declared as setuptools package data:

```toml
[tool.setuptools.package-data]
"ppa.db" = ["migrations/*.sql"]
```

A permanent test builds a normal wheel, installs it into an isolated target, opens a fresh catalogue through the installed package, and proves that all migrations are present and schema bootstrap succeeds.

## Source-safety regression attacks

Permanent regressions now cover:

- archive export temp pathname replaced with a hard link to a source photograph;
- Phase-14.0 pending preservation pathname replaced with a hard link to the trusted donor;
- Phase-14.1 pending donor pathname replaced with a hard link to the suspect target;
- thumbnail temporary pathname replaced with a hard link to the source photograph;
- `KeyboardInterrupt` after donor installation but before checkpoint creation;
- crash-style stranded donor artifacts followed by explicit orphan reconciliation;
- direct DELETE and parent-cascade attempts against recovery checkpoints;
- installed-wheel fresh-schema bootstrap.

In every substitution case, the source photograph remains byte-for-byte unchanged.

## Authority boundary

Phase 14.1.1 does **not** add target-replacement authority. The donor may still only be copied into protected operational staging. The target and original donor remain read-only, and recovery execution remains unauthorized.
