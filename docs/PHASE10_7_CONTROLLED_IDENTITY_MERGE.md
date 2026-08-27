# Phase 10.7 — Controlled Identity Merge

Phase 10.7 permits one narrowly controlled repair: two logical Photos that Phase 10.6 proves are currently byte-equivalent and free of independent identity-dependent meaning may be merged after explicit human selection of the surviving Photo identity.

## Preconditions

A merge is considered only when the Phase 10.6 investigation remains eligible. In addition to the existing guards, human `photos.notes` and prior controlled merge history are blockers. PPA never chooses a survivor automatically.

## Commit protocol

1. Build a merge plan from the reviewed competing SHA-256 and explicit survivor Photo ID.
2. Fingerprint every current File under both logical Photos, including File owner, Library, SHA-256, current revision and presence state.
3. On Apply, acquire `BEGIN IMMEDIATE`.
4. Rebuild the Phase 10.6 investigation and merge plan under the write lock.
5. Require the exact evidence fingerprint, retired identity and moved File cohort to match.
6. Reassign only the retired Photo's physical File records to the survivor.
7. Delete the now-empty retired logical Photo row.
8. Append `identity_merge_history` and commit atomically.

No source path, bytes, EXIF, FileRevision evidence, chronology, Events, Albums, Tags, or lineage are changed.

## Deliberate non-features

This is not a generic semantic merge. PPA does not combine notes, Albums, Tags, lineage, chronology or Event interpretation, and it never selects an original/master identity. Any independent meaning blocks automatic merge eligibility.
