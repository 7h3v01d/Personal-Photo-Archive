# Phase 9.8 — Organisation Health & Curation Gaps

Phase 9.8 is a read-only quality layer over explicit Album/Tag curation. It does not infer membership and never feeds chronology, metadata evidence, anchors, reconstructions, Events, EXIF, or source-file writes.

## Indicators

- **Unorganised** — logical Photos with neither an Album membership nor a Tag.
- **No Album** — Photos with no Album membership, even if Tags exist.
- **No Tags** — Photos with no Tag membership, even if Albums exist.
- **Empty Albums** — durable Albums with zero logical members.
- **Unused Tags** — durable Tags applied to zero logical Photos.
- **Missing-only Album/Tag members** — organisations containing a logical Photo for which the Library currently has no present physical copy.
- **Broken saved discovery views** — recipes containing malformed selector JSON or references to Albums/Tags that no longer exist.

There is deliberately no synthetic organisation-confidence score. Each condition is explicit and traceable.

## Desktop

`Organisation Health` builds the projection off the GUI thread. The three photo-level gaps can be opened as the existing bounded logical-Photo browser. Building those potentially large browse projections also happens off-thread.

## CLI

```text
python -m ppa.cli organization-health <library-id>
python -m ppa.cli organization-health <library-id> --json health.json
```

Schema: `ppa-organization-health/1`.
