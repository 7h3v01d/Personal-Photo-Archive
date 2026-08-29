"""Phase 10.7 — controlled merge of proven competing logical Photo identities.

A merge is identity repair only. It never moves/deletes source files, rewrites
metadata, chooses an original, or combines semantic curation. Eligibility comes
from the Phase-10.6 competing-identity investigation and is re-proved under a
reserved SQLite write transaction before any File ownership changes.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection

from ppa.competing_identity import investigate_competing_identity
from ppa.current_identity import verified_current_sha256_sql
from ppa.physical_observation import require_expected_physical_bytes

IDENTITY_MERGE_PLAN_SCHEMA = "ppa-identity-merge-plan/3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class IdentityMergeFile:
    file_id: str
    photo_id: str
    library_id: int
    filename: str
    path: str
    sha256: str | None
    expected_sha256: str | None
    current_revision_id: str | None
    presence_status: str
    health_status: str


@dataclass(frozen=True)
class IdentityMergePlan:
    schema: str
    library_id: int
    sha256: str
    survivor_photo_id: str
    retired_photo_id: str
    moved_files: tuple[IdentityMergeFile, ...]
    survivor_files: tuple[IdentityMergeFile, ...]
    evidence_fingerprint: str

    @property
    def moved_file_ids(self) -> tuple[str, ...]:
        return tuple(f.file_id for f in self.moved_files)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "library_id": self.library_id,
            "sha256": self.sha256,
            "survivor_photo_id": self.survivor_photo_id,
            "retired_photo_id": self.retired_photo_id,
            "moved_files": [f.__dict__ for f in self.moved_files],
            "survivor_files": [f.__dict__ for f in self.survivor_files],
            "evidence_fingerprint": self.evidence_fingerprint,
        }


@dataclass(frozen=True)
class IdentityMergeResult:
    merge_id: str
    library_id: int
    sha256: str
    survivor_photo_id: str
    retired_photo_id: str
    moved_file_ids: tuple[str, ...]
    created_at: str


def _file_state_rows(conn: Connection, photo_ids: tuple[str, str]):
    verified_expr = verified_current_sha256_sql("f", "r")
    return conn.execute(
        f"""SELECT f.id,f.photo_id,f.library_id,f.filename,f.path,
                   f.sha256 AS expected_sha256,f.current_revision_id,
                   f.presence_status,f.health_status,
                   {verified_expr} AS verified_current_sha256
              FROM files f
              LEFT JOIN file_revisions r ON r.id=f.current_revision_id
             WHERE f.photo_id IN (?,?)
             ORDER BY f.photo_id,f.library_id,f.filename COLLATE NOCASE,f.id""",
        photo_ids,
    ).fetchall()


def _fingerprint(*, library_id: int, sha256: str, survivor: str, retired: str, rows) -> str:
    payload = {
        "library_id": int(library_id),
        "sha256": sha256,
        "survivor_photo_id": survivor,
        "retired_photo_id": retired,
        "files": [
            {
                "id": r["id"], "photo_id": r["photo_id"], "library_id": int(r["library_id"]),
                "path": r["path"],
                "expected_sha256": r["expected_sha256"],
                "verified_current_sha256": r["verified_current_sha256"],
                "current_revision_id": r["current_revision_id"],
                "presence_status": r["presence_status"],
                "health_status": r["health_status"],
            }
            for r in rows
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_identity_merge(conn: Connection, *, library_id: int, sha256: str,
                        survivor_photo_id: str) -> IdentityMergePlan:
    """Freeze a human-selected survivor for one currently merge-eligible conflict."""
    inv = investigate_competing_identity(conn, library_id=library_id, sha256=sha256)
    if not inv.merge_consideration.eligible:
        detail = "; ".join(inv.merge_consideration.blockers) or inv.merge_consideration.rationale
        raise ValueError(f"competing identities are not eligible for controlled merge: {detail}")
    photo_ids = inv.photo_ids
    if len(photo_ids) != 2 or survivor_photo_id not in photo_ids:
        raise ValueError("survivor Photo must be one of the two reviewed competing logical Photos")
    retired = photo_ids[1] if photo_ids[0] == survivor_photo_id else photo_ids[0]
    rows = _file_state_rows(conn, (survivor_photo_id, retired))
    if not rows:
        raise ValueError("competing logical Photos no longer have physical Files")
    if any(int(r["library_id"]) != int(library_id) for r in rows):
        raise ValueError("controlled merge requires both logical Photos to remain confined to the reviewed Library")
    if any(r["verified_current_sha256"] != sha256 for r in rows):
        raise ValueError("controlled merge requires every relevant File to have verified current content matching the reviewed SHA-256")
    moved_rows = [r for r in rows if r["photo_id"] == retired]
    survivor_rows = [r for r in rows if r["photo_id"] == survivor_photo_id]
    if not moved_rows or not survivor_rows:
        raise ValueError("both competing logical Photos must still own at least one physical File")
    def cv(r):
        return IdentityMergeFile(r["id"], r["photo_id"], int(r["library_id"]), r["filename"], r["path"],
                                 r["verified_current_sha256"], r["expected_sha256"],
                                 r["current_revision_id"], r["presence_status"], r["health_status"])
    return IdentityMergePlan(
        IDENTITY_MERGE_PLAN_SCHEMA, int(library_id), sha256, survivor_photo_id, retired,
        tuple(cv(r) for r in moved_rows), tuple(cv(r) for r in survivor_rows),
        _fingerprint(library_id=library_id, sha256=sha256, survivor=survivor_photo_id,
                     retired=retired, rows=rows),
    )


def execute_identity_merge(conn: Connection, plan: IdentityMergePlan, *, note: str | None = None) -> IdentityMergeResult:
    """Re-prove eligibility under ``BEGIN IMMEDIATE`` and atomically merge identities."""
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise ValueError("identity-merge note is too long")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = plan_identity_merge(conn, library_id=plan.library_id, sha256=plan.sha256,
                                        survivor_photo_id=plan.survivor_photo_id)
        except ValueError as exc:
            raise ValueError("identity merge plan is stale; refresh the competing-identity investigation and review again") from exc
        if (fresh.evidence_fingerprint != plan.evidence_fingerprint or
                fresh.retired_photo_id != plan.retired_photo_id or
                fresh.moved_file_ids != plan.moved_file_ids):
            raise ValueError("identity merge plan is stale; refresh the competing-identity investigation and review again")
        relevant_files = (*fresh.survivor_files, *fresh.moved_files)
        attestation_inputs = tuple((f.file_id, f.path, str(f.sha256)) for f in relevant_files if f.sha256)
        if len(attestation_inputs) != len(relevant_files):
            raise ValueError("identity merge requires verified current content for every relevant physical File")
        before_physical = require_expected_physical_bytes(attestation_inputs, context="identity merge")
        placeholders = ",".join("?" for _ in fresh.moved_file_ids)
        cur = conn.execute(
            f"UPDATE files SET photo_id=? WHERE id IN ({placeholders}) AND photo_id=?",
            (fresh.survivor_photo_id, *fresh.moved_file_ids, fresh.retired_photo_id),
        )
        if cur.rowcount != len(fresh.moved_file_ids):
            raise ValueError("identity merge lost ownership of one or more physical Files")
        # Eligibility excludes identity-dependent references/history which would
        # require semantic reconciliation. The retired Photo should therefore be empty.
        cur = conn.execute("DELETE FROM photos WHERE id=?", (fresh.retired_photo_id,))
        if cur.rowcount != 1:
            raise ValueError("retired logical Photo could not be removed")
        merge_id = str(uuid.uuid4()); now = _now()
        conn.execute(
            "INSERT INTO identity_merge_history(merge_id,action,library_id,sha256,survivor_photo_id,"
            "retired_photo_id,moved_file_ids_json,evidence_fingerprint,note,created_at) "
            "VALUES (?,'merge_competing_identity',?,?,?,?,?,?,?,?)",
            (merge_id, fresh.library_id, fresh.sha256, fresh.survivor_photo_id,
             fresh.retired_photo_id, json.dumps(list(fresh.moved_file_ids), separators=(",", ":")),
             fresh.evidence_fingerprint, note, now),
        )
        after_physical = require_expected_physical_bytes(attestation_inputs, context="identity merge")
        if after_physical != before_physical:
            raise ValueError("identity merge: physical File changed during execution; run Verify / refresh investigation")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return IdentityMergeResult(merge_id, fresh.library_id, fresh.sha256, fresh.survivor_photo_id,
                               fresh.retired_photo_id, fresh.moved_file_ids, now)


def list_identity_merges(conn: Connection, *, photo_id: str | None = None):
    if photo_id is None:
        return conn.execute("SELECT * FROM identity_merge_history ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM identity_merge_history WHERE survivor_photo_id=? OR retired_photo_id=? ORDER BY id",
        (photo_id, photo_id),
    ).fetchall()
