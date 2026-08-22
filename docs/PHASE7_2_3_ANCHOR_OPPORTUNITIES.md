# Phase 7.2.3 — Anchor Opportunity Detection

Phase 7.2.3 identifies the most valuable **question to ask the human next**. It is a workflow/read-model layer only: it never guesses a calendar date and never creates an anchor automatically.

## Safety contract

- Only strong-device reset runs are eligible for propagation leverage.
- Model-only/ambiguous camera groups are never presented as high-leverage anchor opportunities.
- A group that already has an effective exact human anchor is not asked for another one; the next action there is reconstruction refresh/review.
- A candidate already covered by an effective human anchor is not selected.
- Every opportunity lists the exact dependent file IDs it could affect.
- Opportunity IDs are stable hashes of group membership, not ephemeral reset labels.
- Ranking is deterministic and read-only.

## CLI

```text
python -m ppa.cli pilot questions <library_id>
python -m ppa.cli pilot questions <library_id> --directory 2001-2006
python -m ppa.cli pilot questions <library_id> --json anchor-opportunities.json
```

The first item is the current best question. Date Review consumes the same planner; when a high-leverage question reaches the top of the queue the UI labels it as **Best date question** and reports how many other unresolved photographs could benefit from an exact human date.

## Deliberate non-feature

7.2.3 does not silently infer, create, or confirm anchors. The user remains the authority for human calendar evidence. Interactive anchor entry can be layered on this planner without changing the accepted chronology engines.
