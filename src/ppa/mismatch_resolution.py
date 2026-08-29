"""Phase 12.4 — controlled, human-reviewed hash-mismatch resolution.

Machine health and human disposition are deliberately separate.  Verify owns the
objective ``hash_mismatch`` health fact.  A human may then record one of three
review outcomes:

* retain the expected immutable revision and mark recovery as still needed;
* explicitly adopt the reviewed current bytes as a NEW immutable revision; or
* record that the review remains unresolved.

Only adoption changes catalogue content authority.  Even then, source bytes are
read-only: this module never writes, renames, moves, repairs or deletes a photo.
Every action is bound to the exact evidence shown to the reviewer and revalidated
again under ``BEGIN IMMEDIATE`` before the catalogue is changed.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from ppa.hashing import sha256_file  # compatibility export for existing tests/monkeypatches
from ppa.physical_observation import (PhysicalObservationError, StableFileObservation as CurrentObservation,
                                      observe_stable_image)

MISMATCH_RESOLUTION_PLAN_SCHEMA = "ppa-mismatch-resolution-plan/2"

ACTION_RETAIN_EXPECTED = "retain_expected_recovery_needed"
ACTION_ADOPT_CURRENT = "adopt_current_revision"
ACTION_UNRESOLVED = "reviewed_unresolved"
ACTIONS = {ACTION_RETAIN_EXPECTED, ACTION_ADOPT_CURRENT, ACTION_UNRESOLVED}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fmt_mtime(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class MismatchResolutionPlan:
    schema: str
    decision_id: str
    action: str
    file_id: str
    photo_id: str
    library_id: int
    path: str
    expected_revision_id: str
    expected_sha256: str
    reviewed_observation_id: int | None
    current_state: str
    current_sha256: str | None
    current_size_bytes: int | None
    current_mtime_ns: int | None
    evidence_fingerprint: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass(frozen=True)
class MismatchResolutionResult:
    resolution_id: str
    action: str
    file_id: str
    expected_revision_id: str
    expected_sha256: str
    adopted_revision_id: str | None
    adopted_sha256: str | None
    resolved_at: str


def _latest_mismatch_observation(conn: Connection, file_id: str, expected_revision_id: str):
    return conn.execute(
        """
        SELECT id, expected_revision_id, expected_sha256, observed_sha256, observed_at
          FROM integrity_mismatch_observations
         WHERE file_id=? AND expected_revision_id=?
         ORDER BY id DESC
         LIMIT 1
        """,
        (file_id, expected_revision_id),
    ).fetchone()


def _file_state(conn: Connection, file_id: str):
    return conn.execute(
        """
        SELECT f.id, f.photo_id, f.library_id, f.path, f.filename,
               f.health_status, f.presence_status, f.current_revision_id,
               r.sha256 AS expected_sha256, r.superseded_at AS expected_superseded_at
          FROM files f
          LEFT JOIN file_revisions r ON r.id=f.current_revision_id
         WHERE f.id=?
        """,
        (file_id,),
    ).fetchone()


def _observe_current(path: Path, expected_sha256: str) -> CurrentObservation:
    """Read one stable current-byte observation without ever opening for write."""
    try:
        return observe_stable_image(path, expected_sha256=expected_sha256, hash_file=sha256_file)
    except PhysicalObservationError as exc:
        raise ValueError("current file changed while it was being revalidated; investigate again") from exc


def _fingerprint(*, row, latest, observation: CurrentObservation) -> str:
    payload = {
        "file_id": row["id"],
        "photo_id": row["photo_id"],
        "library_id": int(row["library_id"]),
        "path": row["path"],
        "health_status": row["health_status"],
        "presence_status": row["presence_status"],
        "current_revision_id": row["current_revision_id"],
        "expected_sha256": row["expected_sha256"],
        "expected_superseded_at": row["expected_superseded_at"],
        "latest_mismatch_observation": None if latest is None else {
            "id": int(latest["id"]),
            "expected_revision_id": latest["expected_revision_id"],
            "expected_sha256": latest["expected_sha256"],
            "observed_sha256": latest["observed_sha256"],
            "observed_at": latest["observed_at"],
        },
        "current": {
            "state": observation.state,
            "sha256": observation.sha256,
            "size_bytes": observation.size_bytes,
            "mtime_ns": observation.mtime_ns,
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validated_snapshot(conn: Connection, file_id: str) -> tuple[object, object, CurrentObservation, str]:
    row = _file_state(conn, file_id)
    if row is None:
        raise ValueError("unknown File")
    if row["health_status"] != "hash_mismatch":
        raise ValueError("File is not currently flagged with a verified hash mismatch")
    if not row["current_revision_id"] or not row["expected_sha256"]:
        raise ValueError("File has no hashed current FileRevision to resolve")
    if row["expected_superseded_at"] is not None:
        raise ValueError("current FileRevision is already marked superseded; catalogue requires repair before resolution")
    latest = _latest_mismatch_observation(conn, file_id, row["current_revision_id"])
    observation = _observe_current(Path(row["path"]), str(row["expected_sha256"]))
    fingerprint = _fingerprint(row=row, latest=latest, observation=observation)
    return row, latest, observation, fingerprint


def plan_mismatch_resolution(
    conn: Connection,
    *,
    file_id: str,
    action: str,
    reviewed_expected_revision_id: str,
    reviewed_expected_sha256: str,
    reviewed_current_state: str,
    reviewed_current_sha256: str | None,
    reviewed_observation_id: int | None,
) -> MismatchResolutionPlan:
    """Bind a requested action to exactly the forensic evidence the user reviewed."""
    if action not in ACTIONS:
        raise ValueError("unknown mismatch-resolution action")
    row, latest, current, fingerprint = _validated_snapshot(conn, file_id)
    latest_id = int(latest["id"]) if latest is not None else None

    if row["current_revision_id"] != reviewed_expected_revision_id:
        raise ValueError("review is stale: the FileRevision changed; investigate again")
    if row["expected_sha256"] != reviewed_expected_sha256:
        raise ValueError("review is stale: the expected SHA-256 changed; investigate again")
    if latest_id != reviewed_observation_id:
        raise ValueError("review is stale: Verify recorded newer mismatch evidence; investigate again")
    if current.state != reviewed_current_state or current.sha256 != reviewed_current_sha256:
        raise ValueError("review is stale: current on-disk bytes changed; investigate again")
    if current.state == "matches_expected":
        raise ValueError("current bytes now reproduce the expected revision; run Verify instead of resolving the old mismatch")
    if action == ACTION_ADOPT_CURRENT:
        if current.state != "still_mismatched" or not current.sha256:
            raise ValueError("current bytes can be adopted only when they are a stable, decodable image with a reviewed SHA-256")
        if current.sha256 == row["expected_sha256"]:
            raise ValueError("current bytes already match the expected revision; adoption would create a false revision")

    return MismatchResolutionPlan(
        MISMATCH_RESOLUTION_PLAN_SCHEMA,
        str(uuid.uuid4()),
        action,
        row["id"],
        row["photo_id"],
        int(row["library_id"]),
        row["path"],
        row["current_revision_id"],
        row["expected_sha256"],
        latest_id,
        current.state,
        current.sha256,
        current.size_bytes,
        current.mtime_ns,
        fingerprint,
    )


def execute_mismatch_resolution(
    conn: Connection,
    plan: MismatchResolutionPlan,
    *,
    note: str | None = None,
) -> MismatchResolutionResult:
    """Re-prove the reviewed evidence, then record/execute the decision atomically."""
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise ValueError("mismatch-resolution note is too long")
    if plan.schema != MISMATCH_RESOLUTION_PLAN_SCHEMA or plan.action not in ACTIONS:
        raise ValueError("invalid mismatch-resolution plan")

    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM integrity_mismatch_resolutions WHERE decision_id=?",
            (plan.decision_id,),
        ).fetchone() is not None:
            raise ValueError("mismatch-resolution plan has already been executed; review again for a new decision")
        row, latest, current, fingerprint = _validated_snapshot(conn, plan.file_id)
        latest_id = int(latest["id"]) if latest is not None else None
        if (
            fingerprint != plan.evidence_fingerprint
            or row["current_revision_id"] != plan.expected_revision_id
            or row["expected_sha256"] != plan.expected_sha256
            or row["path"] != plan.path
            or latest_id != plan.reviewed_observation_id
            or current.state != plan.current_state
            or current.sha256 != plan.current_sha256
            or current.size_bytes != plan.current_size_bytes
            or current.mtime_ns != plan.current_mtime_ns
        ):
            raise ValueError("mismatch-resolution plan is stale; investigate and review again")
        if current.state == "matches_expected":
            raise ValueError("current bytes now match the expected revision; run Verify instead")

        resolution_id = str(uuid.uuid4())
        resolved_at = _now()
        adopted_revision_id: str | None = None
        adopted_sha256: str | None = None

        if plan.action == ACTION_ADOPT_CURRENT:
            if current.state != "still_mismatched" or not current.sha256:
                raise ValueError("reviewed current bytes are not eligible for adoption")
            adopted_revision_id = uuid.uuid4().hex
            adopted_sha256 = current.sha256
            # The old FileRevision remains immutable; only its lifecycle marker
            # changes as the pointer moves to the newly human-authorised revision.
            conn.execute(
                "UPDATE file_revisions SET superseded_at=? WHERE id=? AND superseded_at IS NULL",
                (resolved_at, plan.expected_revision_id),
            )
            conn.execute(
                """
                INSERT INTO file_revisions(
                    id,file_id,sha256,size_bytes,width_px,height_px,fs_mtime,
                    first_observed_at,observed_session,extraction_status
                ) VALUES (?,?,?,?,?,?,?,?,NULL,'pending')
                """,
                (
                    adopted_revision_id, plan.file_id, current.sha256, current.size_bytes,
                    current.width_px, current.height_px, current.fs_mtime, resolved_at,
                ),
            )
            conn.execute(
                """
                UPDATE files
                   SET current_revision_id=?, sha256=?, hash_computed_at=?, size_bytes=?,
                       fs_mtime=?, width_px=?, height_px=?, mime_type=?, camera_id=NULL,
                       status='active', presence_status='present', health_status='ok'
                 WHERE id=?
                """,
                (
                    adopted_revision_id, current.sha256, resolved_at, current.size_bytes,
                    current.fs_mtime, current.width_px, current.height_px, current.mime_type,
                    plan.file_id,
                ),
            )
            if current.fs_mtime:
                conn.execute(
                    "INSERT INTO metadata_observations(file_id,file_revision_id,source,key,value,session_id) "
                    "VALUES (?,?,'filesystem','mtime',?,NULL)",
                    (plan.file_id, adopted_revision_id, current.fs_mtime),
                )
            event_type = "hash_mismatch_resolved_adopted"
            detail = (
                f"Human-reviewed mismatch resolution {resolution_id}: current bytes were explicitly "
                f"adopted as a new immutable FileRevision. expected_sha256={plan.expected_sha256} "
                f"adopted_sha256={current.sha256}. Source file was not modified by PPA."
            )
        elif plan.action == ACTION_RETAIN_EXPECTED:
            event_type = "hash_mismatch_recovery_needed"
            detail = (
                f"Human-reviewed mismatch resolution {resolution_id}: expected FileRevision retained; "
                "current bytes were not adopted and recovery remains needed. "
                f"expected_sha256={plan.expected_sha256} observed_sha256={current.sha256 or 'unavailable'}."
            )
        else:
            event_type = "hash_mismatch_reviewed_unresolved"
            detail = (
                f"Human-reviewed mismatch resolution {resolution_id}: mismatch left unresolved; "
                "catalogue authority unchanged. "
                f"expected_sha256={plan.expected_sha256} observed_sha256={current.sha256 or 'unavailable'}."
            )

        conn.execute(
            """
            INSERT INTO integrity_mismatch_resolutions(
                resolution_id,decision_id,file_id,action,expected_revision_id,expected_sha256,
                reviewed_observation_id,reviewed_current_state,reviewed_current_sha256,
                observed_path,observed_size_bytes,observed_mtime_ns,
                adopted_revision_id,adopted_sha256,evidence_fingerprint,note,resolved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                resolution_id, plan.decision_id, plan.file_id, plan.action, plan.expected_revision_id,
                plan.expected_sha256, plan.reviewed_observation_id, plan.current_state,
                plan.current_sha256, plan.path, plan.current_size_bytes, plan.current_mtime_ns,
                adopted_revision_id, adopted_sha256, plan.evidence_fingerprint, note, resolved_at,
            ),
        )
        conn.execute(
            "INSERT INTO integrity_events(file_id,event_type,detail) VALUES (?,?,?)",
            (plan.file_id, event_type, detail),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return MismatchResolutionResult(
        resolution_id, plan.action, plan.file_id, plan.expected_revision_id,
        plan.expected_sha256, adopted_revision_id, adopted_sha256, resolved_at,
    )


def list_mismatch_resolutions(conn: Connection, *, file_id: str | None = None):
    if file_id is None:
        return conn.execute(
            "SELECT * FROM integrity_mismatch_resolutions ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM integrity_mismatch_resolutions WHERE file_id=? ORDER BY id",
        (file_id,),
    ).fetchall()


def latest_mismatch_resolution(conn: Connection, file_id: str):
    return conn.execute(
        "SELECT * FROM integrity_mismatch_resolutions WHERE file_id=? "
        "ORDER BY id DESC LIMIT 1",
        (file_id,),
    ).fetchone()
