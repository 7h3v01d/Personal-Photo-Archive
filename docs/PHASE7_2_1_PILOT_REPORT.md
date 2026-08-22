# Phase 7.2.1 — Pilot Analysis Report

Status: implementation slice.

This layer is observational only. It aggregates accepted Archive Core, Phase 6,
and Phase 7 read models and never creates chronology evidence, anchors,
reconstructions, decisions, metadata, or source-file changes.

`ppa pilot report <library_id>` prints a concise summary. `--json PATH` writes the
versioned `ppa-pilot-report/1` structured report. Every aggregate carries the
file ids that produced it so headline counts remain auditable.

Review priority is deterministic and intentionally measures *human leverage*,
not merely uncertainty. A stale decision, an evidence conflict, or one frame
whose confirmation could constrain a large strong-device reset run is Priority
A; isolated uncertainty is lower priority.

## Counting semantics

Reliability and review-priority buckets are partitions: each in-scope File is in
exactly one bucket. Unresolved reasons are also one-primary-reason-per-file after
current confirmed reconstructions are excluded. Conflict categories are flags,
not a partition: one File may legitimately appear in more than one conflict kind
when multiple independent contradictions exist.
