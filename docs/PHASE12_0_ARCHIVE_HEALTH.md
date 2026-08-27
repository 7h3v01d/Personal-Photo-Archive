# Phase 12.0 — Backup & Archive Health Foundation

Phase 12 begins the long-deferred Backup & Archive Health work with a deliberately read-only catalogue projection.

## Scope

For each logical Photo in one registered Library, PPA now reports what the catalogue can currently prove about copy coverage:

- no present catalogued File;
- exactly one present catalogued File;
- two or more healthy present Files sharing one known current SHA-256;
- a mixture of present and missing catalogued copies;
- present Files carrying a health warning;
- present Files without a current SHA-256;
- logical Photos whose present Files currently diverge across multiple known SHA-256 values.

The desktop exposes **Library → Archive Health** and each category can be opened as a bounded logical-Photo browser. The same projection is available from the CLI:

```text
python -m ppa.cli archive-health <library-id>
python -m ppa.cli archive-health <library-id> --json archive-health.json
```

Structured schema: `ppa-archive-health/1`.

## Critical evidence boundary

Phase 12.0 does **not** call multiple catalogued Files “independent backups”. Multiple paths can still live on the same physical device, and distinct directory entries may be hard links to the same underlying file object. Until storage identity is explicitly captured, the strongest safe statement is **multiple exact present catalogued Files**.

This closes the conceptual gap identified in the long-standing backlog without inventing certainty.

## Read-only contract

The projection reads existing Library/File/SHA/presence/health state only. It performs no source-file reads, no hashing, no database writes, no filesystem mutation, and no authority change. Scan and Verify remain the mechanisms that establish current observations.

## Follow-on work

Later Phase-12 slices can add storage/device identity and hard-link awareness, then reason about genuinely independent redundancy. The existing ambiguous-restoration backlog item also remains deferred until the required evidence model is explicit.
