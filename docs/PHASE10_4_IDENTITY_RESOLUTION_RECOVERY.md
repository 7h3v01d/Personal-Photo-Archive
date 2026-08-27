# Phase 10.4 — Identity Resolution Review & Recovery

Phase 10.4 makes every controlled Phase-10.3 identity split reviewable and, only while the split remains provably reversible, safely recombinable.

## Recovery contract

Recovery is **not a general Photo merge**. It reverses one specific audited `split_hash_cohort` operation.

A split is recoverable only while:

- the source and split-created logical Photos still exist;
- the split-created Photo contains exactly the originally moved File IDs;
- those Files remain in the original Library and retain the split SHA-256;
- the source still has physical File representation;
- no later Album/Tag curation touched either logical Photo, even if later removed;
- no later lineage create/remove touched either logical Photo;
- no later identity resolution touched either logical Photo;
- no third logical Photo now owns the same current split SHA-256;
- the split has not already been recombined.

The plan is fingerprinted and rebuilt under `BEGIN IMMEDIATE` immediately before mutation. Stale plans fail closed.

## Audit

Schema v25 adds `identity_resolution_recovery_history`. Recovery never deletes the original split record. A reversible sequence therefore remains visible as:

`split_hash_cohort` → `recombine_split`

## Topology review

The review UI shows the audited moved cohort as the known pre-split fact plus the current source/new-Photo topology. Phase 10.3 did not persist a complete immutable snapshot of every retained File, so Phase 10.4 explicitly refuses to fabricate unavailable historical topology.

## Authority boundary

Recovery only changes catalogue logical-Photo ownership of the exact split Files and retires the now-empty split-created Photo. It never moves or edits source files and does not rewrite EXIF, revisions, metadata observations, chronology, Events, Albums, Tags, or lineage.
