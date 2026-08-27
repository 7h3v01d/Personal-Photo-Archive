# Phase 10.5 — Identity Health & Resolution Queue

Phase 10.5 adds a read-only triage projection over current duplicate identity, divergence, and audited resolution state.

Priority is explicit rather than scored:

- P0 competing identity — identical current bytes assigned to multiple logical Photos.
- P1 identity divergence — one logical Photo contains multiple current known hashes.
- P2 recoverable split — one audited split remains provably reversible.
- P3 review-only split — split history remains inspectable but automatic recovery is no longer safe.
- INFO recombined split — completed recovery retained for audit.

The queue never merges, splits, recombines, creates lineage, deletes Files, or writes source photos. Corrective operations remain delegated to the controlled Phase-10 workflows.
