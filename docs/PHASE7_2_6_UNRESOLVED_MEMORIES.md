# Phase 7.2.6 — Unresolved Memories

Unresolved chronology is a first-class archival state, not an error and not an invitation to invent precision.

`ppa.unresolved` is a read-only classification layer over the accepted Phase 6/7 engines. Every photo without a fresh confirmed reconstruction receives exactly one primary unresolved category, with traceable related file IDs and evidence context.

Primary categories are ordered by review significance:

- **Stale decision needs review** — a previous reconstruction/decision no longer matches current bytes or evidence.
- **Conflicting evidence** — independent chronology evidence disagrees; PPA refuses to choose automatically.
- **Reset run without an exact human anchor** — strong single-device sequence evidence exists, but no exact human calendar witness can establish the clock offset.
- **Range-only date knowledge** — evidence supports only a range, and PPA preserves that imprecision.
- **Reconstruction awaiting human review** — a current proposal exists but has not been confirmed.
- **Questionable date without corroboration** — the recorded chronology is doubtful without enough independent support to reconstruct safely.
- **No reconstruction available** — chronology is not confirmed and Phase 7 has no safe interpretation.
- **No usable date evidence** — no observed capture-date metadata is available; filesystem time alone is not promoted to photographic truth.

Fresh confirmed reconstructions are deliberately absent from this view.

## Interfaces

CLI:

```text
python -m ppa.cli pilot unresolved <library-id>
python -m ppa.cli pilot unresolved <library-id> --directory 2001-2006
python -m ppa.cli pilot unresolved <library-id> --json unresolved.json
```

Desktop: **Unresolved Memories** builds the view on a background worker and opens the existing provenance-aware viewer in deterministic category order. The banner explains why each photograph remains unresolved. This view does not create anchors or decisions.

JSON schema: `ppa-unresolved-memories/1`.
