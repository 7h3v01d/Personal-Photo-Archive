# Phase 7.2.7 — Pilot Audit Report

Phase 7.2.7 closes the Phase-7.2 pilot workflow with a read-only audit snapshot.
It measures what the accepted chronology/reconstruction stack currently knows;
it does not create chronology facts or mutate source files/catalogue evidence.

## Truthful baseline rule

A current catalogue cannot prove what the archive looked like before an earlier
human-review session. PPA therefore never fabricates a historical baseline.
`pilot audit` captures an explicit versioned snapshot. A truthful before/after
comparison requires two snapshots from the same scope.

## Snapshot measures

- usable chronology (healthy recorded dates plus fresh human-confirmed reconstruction)
- usable recorded chronology
- fresh confirmed and proposed reconstructions
- stale reconstruction decisions/dependencies
- unresolved memories and their categories
- chronology conflicts
- actionable review workload
- high-leverage anchor questions and maximum leverage
- current integrity flags
- audit source-photo writes (always zero by contract)

All headline state metrics retain the contributing file IDs for traceability.

## CLI

```text
python -m ppa.cli pilot audit 1
python -m ppa.cli pilot audit 1 --directory 2001-2006
python -m ppa.cli pilot audit 1 --json before.json
python -m ppa.cli pilot audit-compare before.json after.json
python -m ppa.cli pilot audit-compare before.json after.json --json delta.json
```

Schemas:

- `ppa-pilot-audit/1`
- `ppa-pilot-audit-comparison/1`

Cross-scope comparisons fail closed.

## Desktop

The **Pilot Audit** toolbar action runs the analysis on a worker-owned SQLite
connection, remains cancellable, and shows the headline outcome with the full
traceable report available in the details panel.

## Performance

The audit performs one expensive Phase-6/7 pilot analysis and reuses that report
when building the unresolved and review-queue views. It does not repeat the
collection-wide chronology pass for each component.
