# Phase 10.6 — Competing Identity Investigation

Phase 10.6 adds a read-only forensic investigation for the Phase-10 P0 condition where the same known current SHA-256 is owned by Files attached to multiple logical Photos in one Library.

## Evidence classifications

- `BYTE_IDENTICAL_WHEN_FIRST_OBSERVED` — immutable revision evidence shows the competing Files were already byte-identical when PPA first observed them. This does not explain how or why separate logical identities were created.
- `CONVERGED_AFTER_OBSERVED_CHANGE` — PPA observed at least one physical File change from a different revision hash into the shared current bytes. This proves convergence after observation, not which identity is correct or whether one derives from another.
- `INSUFFICIENT_HISTORY` — current bytes compete but immutable revision history is incomplete.

## Merge consideration is not a merge

The investigation may say a future controlled merge can be *considered* only when exactly two logical Photos are involved, all known Files under both identities have the same current SHA, no current hash is unknown, both identities are confined to the reviewed Library, and no Album/Tag, lineage, or prior identity-resolution history gives either identity independent meaning.

Any blocker makes the case `review_only`. Phase 10.6 never merges, chooses a winner, copies organisation, creates lineage, changes chronology, deletes Files, or writes source-photo bytes.

## Interfaces

Desktop: `Duplicates & Lineage` → `Identity Health` → select a P0 row → `Investigate competing identity…`.

CLI: `python -m ppa.cli competing-identity-investigation <library-id> <sha256> [--json out.json]`.

Schema: `ppa-competing-identity-investigation/1`.
