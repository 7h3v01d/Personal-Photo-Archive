# Phase 7.3 — Real-Collection Pilot Harness

Phase 7.3 turns the read-only Phase-7.2 audit machinery into a durable pilot
session that can be safely resumed across application runs.

## Contract

A pilot session never edits source photos, EXIF, observations, anchors, or
reconstruction decisions. It only captures explicit audit snapshots around the
normal review workflow.

The baseline is captured once and retained unchanged. Optional checkpoints are
append-only observations. Closing captures a final snapshot and compares it to
the original baseline. All comparisons are scope checked.

Each baseline/checkpoint/final snapshot carries a SHA-256 digest of its canonical
JSON. A changed or corrupted session file fails closed on load.

## CLI

```bash
python -m ppa.cli pilot session-start 1 pilot-2001-2006.json --directory 2001-2006
python -m ppa.cli pilot session-status pilot-2001-2006.json
python -m ppa.cli pilot session-checkpoint pilot-2001-2006.json --label "after first review"
python -m ppa.cli pilot session-close pilot-2001-2006.json
```

The session file is written atomically. `session-start` refuses to overwrite an
existing session path.

## Why external JSON rather than a schema migration?

The pilot is measurement around the catalogue, not catalogue truth. Keeping the
session as a versioned external artifact makes its baseline portable, reviewable,
and explicitly separate from the archive's evidence database. Phase 7 inference
and persistence schemas remain frozen.
