# Phase 12.1 — Filesystem Storage Identity & Hard-Link Awareness

Phase 12.1 closes the long-standing hard-link ambiguity in Backup & Archive
Health without weakening the archive's evidence boundaries.

## What the scanner now observes

Every successful supported-file inventory already performs a read-only
`stat()`. Phase 12.1 retains three additional facts from that observation when
the platform exposes them:

- filesystem/device identifier (`st_dev`) as an opaque text token;
- filesystem object identifier (`st_ino` / platform file index) as an opaque
  text token;
- link count (`st_nlink`) when available.

The pair **(device id, object id)** is the current filesystem-object identity.
Two different catalogue paths with the same pair are directory entries for the
same underlying filesystem object: in ordinary filesystems, hard links.

The scanner writes only to the catalogue. It does not modify source files.

## Fail-closed upgrade behaviour

Migration 027 adds current storage-identity fields to `files` and a sparse
`file_storage_identity_history` ledger. Existing Files are migrated with NULL
identity fields. No SQL migration attempts to inspect source paths or invent
historic device/object identity.

A normal re-scan establishes the current identity. If a platform or scan cannot
establish both device and object id, current identity remains unknown and
Archive Health refuses to infer object/device redundancy from stale or partial
evidence.

History is appended only for:

- storage identity becoming established;
- a change of device/object identity;
- storage identity becoming unavailable;
- a change of observed link count.

Routine unchanged rescans refresh the current observation timestamp/session
without appending identical history indefinitely.

## Archive Health schema v2

Structured output is now `ppa-archive-health/2`.

In addition to Phase-12.0 coverage categories, Archive Health reports:

- **Exact sets with unknown storage identity** — at least one member of an
  otherwise healthy byte-exact set lacks current device/object evidence;
- **Exact sets with hard-link path inflation** — at least two catalogue paths
  share one observed filesystem object, so path count overstates object count;
- **Exact sets spanning distinct filesystem objects** — all members have known
  identity and at least two distinct device/object pairs exist;
- **Exact sets spanning distinct filesystem device IDs** — all members have
  known identity and the OS reports more than one device id.

The categories intentionally overlap. For example, three paths can contain one
hard-linked pair plus a genuinely distinct second filesystem object.

## Evidence boundary

Phase 12.1 still does **not** use the phrase "independent backup" as a proved
fact.

A distinct filesystem object is stronger than a second path, and a distinct
filesystem device id is stronger than a second object on one device. Neither,
by itself, proves separate physical hardware, controllers, power, enclosure,
location, cloud account, or other independent failure domains.

This phase therefore improves redundancy accounting while preserving the rule:
**do not manufacture certainty that the available evidence cannot support.**

## User surfaces

The existing **Library → Archive Health** action now includes the Phase-12.1
storage-identity categories and keeps every category browsable as logical
Photos. CLI remains:

```text
python -m ppa.cli archive-health <library-id>
python -m ppa.cli archive-health <library-id> --json archive-health.json
```

No new destructive command or automatic backup operation is introduced.
