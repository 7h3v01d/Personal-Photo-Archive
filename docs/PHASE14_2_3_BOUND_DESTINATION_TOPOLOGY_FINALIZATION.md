# Phase 14.2.3 — Bound Destination Topology Finalization

## Purpose

Phase 14.2.2 proved the persisted registered Library-root identity both initially and after operational-evidence hashing. Adversarial review then demonstrated a narrower race inside that final attestation: the root and target-parent pathname checks could succeed, after which either directory pathname could be replaced while the final target stable-content hash was still running.

14.2.3 closes that pre-finalization window without granting target-write or recovery-execution authority.

## Bound finalization contract

After all Phase-14 operational evidence has been re-attested, readiness performs the following sequence:

```text
re-establish registered Library root + known parent policy
        ↓
bind exact registered Library root object
        ↓
require bound root identity == persisted/initial root identity
        ↓
bind exact target-parent object
        ↓
require bound parent identity == verified/initial parent identity
        ↓
retain both directory pins open
        ↓
final stable target content/identity/link-count observation
        ↓
root authority verify_pathname()
        ↓
parent authority verify_pathname()
        ↓
exact initial/final target snapshot comparison
        ↓
readiness fingerprint / optional SQLite record
```

On POSIX the pins are descriptor-bound `BoundDirectory` objects. On Windows they are native-handle `WindowsDirectoryPin` objects. Both expose exact filesystem identity and pathname freshness verification.

A substitution between the preliminary policy check and binding is caught because the bound identity must equal the already accepted root/parent identity. A substitution after binding but during target hashing is caught by `verify_pathname()` before readiness fingerprint construction.

## Read-only use of authority primitives

Phase 14.2.3 does **not** use any child mutation operation exposed by the directory-authority objects. The handles/descriptors are used only as read-only identity/freshness pins and are closed in `finally` regardless of success or failure.

Unchanged authority flags:

```text
target_replacement_authorized = false
recovery_execution_authorized = false
```

No target create, replace, rename, delete, chmod, metadata repair, timestamp repair or EXIF write is introduced. Schema remains v40 with 40 migrations.

## Permanent regressions

14.2.3 adds four adversarial regressions using the production bound-directory path:

1. final root binding succeeds, then during the final target observation the registered root is renamed away, a replacement root is created, and the genuine known parent is transplanted beneath it; readiness must fail and no row is created;
2. final parent binding succeeds, then during the final target observation the genuine parent is moved away, a replacement parent is created, and the exact target object is transplanted beneath it; readiness must fail;
3. the same root interleaving during the record-time rebuild under `BEGIN IMMEDIATE` commits neither a readiness row nor a readiness-recorded event;
4. the same parent interleaving during the record-time rebuild commits neither a readiness row nor a readiness-recorded event.

## Residual timing boundary

There remains, as with any filesystem/SQLite design, a microscopic interval after the final pathname observations and before SQLite commit. Phase 14.2.3 performs no filesystem mutation and grants no execution authority, so that post-observation drift remains a later re-attestation concern rather than a Phase-14.2 source-safety boundary. A future execution phase must independently bind and re-attest again.
