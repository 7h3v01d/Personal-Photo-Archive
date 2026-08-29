"""Verified-current content identity semantics.

Phase 12.4.1 establishes a hard distinction between immutable catalogue truth
and what PPA is currently entitled to assert about bytes on disk.

``files.sha256`` is a compatibility mirror of the current immutable
``FileRevision``.  After Verify detects a mismatch it intentionally remains the
*expected* SHA-256; therefore it must never be used by identity-changing logic
as proof of current bytes.

A File has a ``verified_current_sha256`` only when all of the following remain
true at the same time:

* the File is present;
* machine health is ``ok``;
* ``current_revision_id`` resolves to a FileRevision owned by that File;
* that revision is not superseded;
* the File compatibility mirror equals the revision SHA-256; and
* the revision SHA-256 is known.

Missing, unreadable, hash-mismatched, unknown-health, incoherent, or unhashed
Files therefore have UNKNOWN current-byte identity for logical-identity
purposes.  Forensic mismatch observations remain separate evidence and are not
substituted here.
"""
from __future__ import annotations

from collections.abc import Mapping


def verified_current_sha256_sql(file_alias: str = "f", revision_alias: str = "r") -> str:
    """Return the canonical SQL expression for verified-current SHA-256.

    Callers should join ``file_revisions`` as ``revision_alias`` on
    ``{file_alias}.current_revision_id`` and alias the expression as
    ``verified_current_sha256``.
    """
    f = file_alias
    r = revision_alias
    return (
        "CASE WHEN "
        f"{f}.presence_status='present' "
        f"AND {f}.health_status='ok' "
        f"AND {f}.current_revision_id={r}.id "
        f"AND {r}.file_id={f}.id "
        f"AND {r}.superseded_at IS NULL "
        f"AND {f}.sha256={r}.sha256 "
        f"AND {r}.sha256 IS NOT NULL AND {r}.sha256<>'' "
        f"THEN {r}.sha256 ELSE NULL END"
    )


def verified_current_sha256_from_row(row: Mapping[str, object]) -> str | None:
    """Return a pre-projected verified-current SHA from a mapping row."""
    value = row["verified_current_sha256"]
    if value is None:
        return None
    value = str(value)
    return value or None


def unverified_current_reason(row: Mapping[str, object]) -> str | None:
    """Explain why a row cannot assert current-byte identity.

    The row must provide the standard File state plus the joined current
    revision fields named ``revision_id``, ``revision_sha256`` and
    ``revision_superseded_at``.  ``None`` means the current identity is verified.
    """
    if row["presence_status"] != "present":
        return "File is not currently present"
    if row["health_status"] != "ok":
        return f"File health is {row['health_status']}"
    if not row["current_revision_id"]:
        return "File has no current FileRevision"
    if row["revision_id"] != row["current_revision_id"]:
        return "current FileRevision cannot be resolved"
    if row["revision_superseded_at"] is not None:
        return "current FileRevision is marked superseded"
    revision_sha = row["revision_sha256"]
    if revision_sha is None or str(revision_sha) == "":
        return "current FileRevision has no SHA-256"
    if row["sha256"] != revision_sha:
        return "File SHA mirror does not match current FileRevision"
    return None
