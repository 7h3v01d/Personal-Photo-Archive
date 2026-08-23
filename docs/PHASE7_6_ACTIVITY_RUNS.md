# Phase 7.6 — Activity Timeline & Run Correlation

Operational diagnostics now carry an optional `run_id`, operation, phase, outcome,
and elapsed time in the structured JSONL log. The Activity Runs desktop view groups
those events into one human-readable run, while CLI commands can list or export a
single sanitized transcript.

This layer is diagnostics only. Run IDs are never chronology evidence, never stored
in the catalogue, and never participate in anchors, reconstructions, or decisions.

## CLI

```text
python -m ppa.cli diagnostics runs
python -m ppa.cli diagnostics run-export <run-id> run.json
```

Run exports redact configured library/home/data paths and explicitly exclude the
catalogue database, source photos, thumbnails, and pilot-session artifacts.
