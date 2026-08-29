# Phase 12.4 — Controlled Hash-Mismatch Resolution

Phase 12.4 turns the Phase-12.3 forensic comparison into an explicit,
human-reviewed resolution workflow without weakening the archive's read-only
source-file contract.

The central rule is that **machine health and human disposition are different
kinds of state**. Verify owns the objective fact that a File is currently
`hash_mismatch`. A human review can record what should happen next, but that
review does not become byte-level verification by changing the health label.

## Three explicit outcomes

A reviewed mismatch can end in exactly one of three actions:

1. **Keep expected / recovery needed** — retain the immutable expected
   FileRevision as catalogue authority. Current bytes are not adopted. The File
   remains `hash_mismatch`, and an append-only resolution/audit record says that
   recovery is still required.
2. **Adopt current as new revision** — explicitly assert that the reviewed
   current bytes are an intentional continuation of the same physical File /
   logical Photo. PPA appends a new immutable FileRevision, supersedes the old
   revision, advances `current_revision_id`, and marks the File healthy against
   that newly adopted identity. The source file itself is never written.
3. **Record unresolved** — record that the discrepancy was reviewed but no
   authority decision was made. Current catalogue authority and
   `hash_mismatch` health remain unchanged.

There is deliberately no automatic policy that chooses among these outcomes.

## Review binding and stale-plan rejection

A resolution request is bound to the exact evidence shown by the Phase-12.3
investigation:

- File id and path;
- expected FileRevision id;
- expected SHA-256;
- most recent structured Verify mismatch observation for that revision;
- freshly observed current-byte state and SHA-256;
- current size and filesystem mtime snapshot.

Before recording a decision, PPA re-hashes/re-observes the source. The resulting
plan carries an evidence fingerprint. Execution then opens `BEGIN IMMEDIATE`,
re-proves the database state and re-reads the current filesystem evidence. Any
change makes the plan stale and aborts the operation.

This prevents a dangerous TOCTOU class where a user reviews bytes A but PPA later
adopts unseen bytes B.

A newer Verify observation also invalidates the reviewed plan even when the hash
is coincidentally unchanged, because the evidence episode itself changed after
review.

## Adoption is conservative

`adopt_current_revision` is permitted only when the current source is:

- present;
- stably hashable during revalidation;
- decodable as an image; and
- still different from the reviewed expected SHA-256.

Missing or unreadable current bytes may be retained-as-expected or left
unresolved, but cannot become a trusted new revision.

If current bytes have returned to the expected SHA-256, PPA refuses all mismatch
resolution actions and directs the user back to **Verify**. Phase 12.4.1 also
prevents an ordinary Scanner reconciliation (including restoration after absence)
from clearing the known mismatch. Verify remains the machine authority that
reconciles the stale mismatch flag back to `ok`.

## New revision semantics

An adoption:

- never mutates the old FileRevision's content facts;
- only sets its lifecycle `superseded_at` marker;
- appends a fresh FileRevision with the revalidated current SHA, dimensions,
  size and filesystem mtime;
- records the new revision's extraction state as `pending`;
- clears the File's cached camera identity until metadata for the new revision is
  extracted;
- records a filesystem mtime observation for the new revision; and
- updates the `files.sha256` compatibility mirror and current file facts.

The new revision is therefore normal archive history, not a rewrite of the
previous truth.

## Append-only resolution ledger — migration 030

Migration 030 adds `integrity_mismatch_resolutions`.

Each row records:

- unique resolution id;
- File id;
- action;
- expected revision id and SHA-256 reviewed by the human;
- structured Verify observation id, when one exists;
- reviewed current state and SHA-256;
- observed path, size and mtime;
- adopted revision id/SHA when adoption occurred;
- evidence fingerprint;
- optional human note; and
- resolution time.

An accompanying `integrity_events` entry keeps the human-readable integrity
history coherent. Phase 12.4.1 gives every reviewed plan a unique `decision_id`:
the same plan can be executed only once. A genuine later review creates a fresh
plan/decision identity and appends a new row; earlier decisions are never erased.

The Phase-12.3 investigation displays the latest resolution only when it belongs
to the **currently expected FileRevision**, so a later mismatch episode cannot
inherit the disposition of an older revision.

## Desktop workflow

The mismatch investigation dialog now exposes:

- **Keep expected / recovery needed…**
- **Record unresolved…**
- **Adopt current as new revision…** (only for a stable, decodable current image)

Every action requires an in-the-moment confirmation and accepts an optional note.
Adoption explicitly states both reviewed hashes and warns that it changes
catalogue authority while leaving the source file untouched.

All hashing and database mutation execute through a background worker rather than
blocking the GUI thread.

## Source safety

Phase 12.4 opens source photographs only for read/stat/hash/decode operations.
It does not write, repair, move, rename, delete, touch timestamps, or rewrite
metadata in any source file.
