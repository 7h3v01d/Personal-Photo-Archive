# Phase 7.2.4 — Evidence Inspector

Status: implemented.

The Evidence Inspector is a read-only explanation layer over the accepted
Phase-6 reliability/reconciliation and Phase-7 reconstruction engines. It does
not infer a new date and does not parse the persisted human-readable evidence
sentence as provenance. Instead it rebuilds the current semantic inputs and
returns a structured trace.

For one present File it reports:

- recorded candidate timestamp and final Phase-6 reliability;
- Phase-6 reasons and explicit evidence conflicts;
- matching reset/order chronology findings;
- exact human, human-range, or GPS independent evidence;
- reset-run membership and whether single-device identity is strong;
- stored reconstruction status, confidence, method and freshness;
- structured method derivation (direct, GPS range, anchor range, clock offset,
  or strong-device bracketing).

The desktop Date Review window exposes the same trace behind **Why?**. Building
it runs on a worker-owned SQLite connection so the viewer remains responsive on
a real collection.

CLI:

```text
python -m ppa.cli pilot explain <file-id>
python -m ppa.cli pilot explain <file-id> --json evidence.json
```

JSON schema: `ppa-evidence-trace/1`.

Safety contract: explanation is observational only. It never writes anchors,
reconstructions, decisions, metadata observations, file records, or source
photographs.
