"""Phase 10.3 — controlled human-reviewed logical Photo identity splitting.

A split is intentionally narrow: one complete current-SHA cohort is moved from
an already-divergent logical Photo into a newly-created logical Photo. The
operation is revalidated under ``BEGIN IMMEDIATE`` and writes only catalogue
identity plus append-only audit history. Source files and immutable revisions
are never changed.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection

from ppa.current_identity import verified_current_sha256_sql
from ppa.physical_observation import require_expected_physical_bytes

IDENTITY_SPLIT_SCHEMA = "ppa-identity-split-plan/3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SplitFile:
    file_id: str
    library_id: int
    filename: str
    path: str
    sha256: str
    expected_sha256: str | None
    current_revision_id: str | None
    presence_status: str
    health_status: str


@dataclass(frozen=True)
class IdentitySplitPlan:
    schema: str
    library_id: int
    source_photo_id: str
    sha256: str
    files: tuple[SplitFile, ...]
    remaining_file_count: int
    remaining_hashes: tuple[str, ...]
    evidence_fingerprint: str

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "library_id": self.library_id,
            "source_photo_id": self.source_photo_id,
            "sha256": self.sha256,
            "files": [f.__dict__ for f in self.files],
            "remaining_file_count": self.remaining_file_count,
            "remaining_hashes": list(self.remaining_hashes),
            "evidence_fingerprint": self.evidence_fingerprint,
        }


@dataclass(frozen=True)
class IdentitySplitResult:
    resolution_id: str
    source_photo_id: str
    new_photo_id: str
    sha256: str
    moved_file_ids: tuple[str, ...]
    created_at: str


def _fingerprint(source_photo_id: str, rows: list) -> str:
    payload = {
        "source_photo_id": source_photo_id,
        "files": [
            {
                "id": r["id"], "library_id": r["library_id"], "photo_id": r["photo_id"],
                "path": r["path"],
                "expected_sha256": r["expected_sha256"],
                "verified_current_sha256": r["verified_current_sha256"],
                "current_revision_id": r["current_revision_id"],
                "presence_status": r["presence_status"], "health_status": r["health_status"],
            }
            for r in sorted(rows, key=lambda x: x["id"])
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_identity_split(conn: Connection, *, library_id: int, source_photo_id: str,
                        file_ids: tuple[str, ...] | list[str]) -> IdentitySplitPlan:
    """Validate and freeze one complete current-hash cohort for a controlled split."""
    ids = tuple(sorted(set(str(x) for x in file_ids if str(x))))
    if not ids:
        raise ValueError("select at least one physical File to split")
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    if conn.execute("SELECT 1 FROM photos WHERE id=?", (source_photo_id,)).fetchone() is None:
        raise ValueError(f"unknown logical Photo {source_photo_id}")

    verified_expr = verified_current_sha256_sql("f", "r")
    rows = conn.execute(
        f"""SELECT f.id,f.library_id,f.photo_id,f.filename,f.path,f.sha256 AS expected_sha256,
                   f.current_revision_id,f.presence_status,f.health_status,
                   {verified_expr} AS verified_current_sha256
              FROM files f
              LEFT JOIN file_revisions r ON r.id=f.current_revision_id
             WHERE f.photo_id=? ORDER BY f.id""", (source_photo_id,),
    ).fetchall()
    if len(rows) < 2:
        raise ValueError("identity split requires the source Photo to have at least two physical Files")
    by_id = {r["id"]: r for r in rows}
    if any(fid not in by_id for fid in ids):
        raise ValueError("every selected File must still belong to the source logical Photo")
    selected = [by_id[fid] for fid in ids]
    if any(r["library_id"] != library_id for r in selected):
        raise ValueError("selected Files must belong to the requested Library")
    if any(r["verified_current_sha256"] is None for r in rows):
        raise ValueError("identity split requires verified current content for every physical File on the source logical Photo")
    hashes = {r["verified_current_sha256"] for r in selected}
    if None in hashes or "" in hashes or len(hashes) != 1:
        raise ValueError("selected Files must share one known current SHA-256")
    sha = next(iter(hashes))

    known_source_hashes = {r["verified_current_sha256"] for r in rows if r["verified_current_sha256"]}
    if len(known_source_hashes) < 2:
        raise ValueError("source logical Photo is not currently hash-divergent")

    other_verified = verified_current_sha256_sql("f", "r")
    competing = conn.execute(
        f"""SELECT DISTINCT f.photo_id FROM files f
              LEFT JOIN file_revisions r ON r.id=f.current_revision_id
             WHERE ({other_verified})=? AND f.photo_id<>? LIMIT 1""",
        (sha, source_photo_id),
    ).fetchone()
    if competing is not None:
        raise ValueError("identical current bytes already belong to another logical Photo; resolve that identity inconsistency before splitting")

    cohort = [r for r in rows if r["verified_current_sha256"] == sha]
    cohort_ids = {r["id"] for r in cohort}
    if any(r["library_id"] != library_id for r in cohort):
        raise ValueError("this hash cohort spans multiple Libraries; cross-Library identity resolution is not permitted in Phase 10.3")
    if set(ids) != cohort_ids:
        outside = cohort_ids - set(ids)
        if outside:
            raise ValueError("split must include the complete current-SHA cohort; identical copies cannot be divided across logical Photos")
        raise ValueError("selected Files do not exactly match one current-SHA cohort")

    remaining = [r for r in rows if r["id"] not in cohort_ids]
    if not remaining:
        raise ValueError("split must leave at least one physical File on the source logical Photo")

    # Refuse a split that would strand existing organisation membership in this
    # Library without any remaining representation of the source Photo.
    if not any(r["library_id"] == library_id for r in remaining):
        org = conn.execute(
            "SELECT 1 FROM album_photos ap JOIN albums a ON a.id=ap.album_id "
            "WHERE ap.photo_id=? AND a.library_id=? "
            "UNION ALL SELECT 1 FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id "
            "WHERE pt.photo_id=? AND t.library_id=? LIMIT 1",
            (source_photo_id, library_id, source_photo_id, library_id),
        ).fetchone()
        if org is not None:
            raise ValueError("split would strand existing Album/Tag membership for the source Photo in this Library")

    split_files = tuple(SplitFile(
        r["id"], r["library_id"], r["filename"], r["path"], r["verified_current_sha256"],
        r["expected_sha256"], r["current_revision_id"], r["presence_status"], r["health_status"]
    ) for r in cohort)
    remaining_hashes = tuple(sorted({r["verified_current_sha256"] for r in remaining if r["verified_current_sha256"]}))
    return IdentitySplitPlan(
        IDENTITY_SPLIT_SCHEMA, library_id, source_photo_id, sha, split_files,
        len(remaining), remaining_hashes, _fingerprint(source_photo_id, rows),
    )


def execute_identity_split(conn: Connection, plan: IdentitySplitPlan, *, note: str | None = None) -> IdentitySplitResult:
    """Revalidate ``plan`` under a reserved write transaction, then split atomically."""
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise ValueError("identity-resolution note is too long")
    file_ids = tuple(f.file_id for f in plan.files)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = plan_identity_split(conn, library_id=plan.library_id,
                                        source_photo_id=plan.source_photo_id, file_ids=file_ids)
        except ValueError as exc:
            raise ValueError("identity split plan is stale; refresh the divergence and review again") from exc
        if fresh.evidence_fingerprint != plan.evidence_fingerprint or fresh.sha256 != plan.sha256:
            raise ValueError("identity split plan is stale; refresh the divergence and review again")
        verified_expr = verified_current_sha256_sql("f", "r")
        physical_rows = conn.execute(
            f"""SELECT f.id,f.path,{verified_expr} AS verified_current_sha256
                  FROM files f
                  LEFT JOIN file_revisions r ON r.id=f.current_revision_id
                 WHERE f.photo_id=? ORDER BY f.id""",
            (fresh.source_photo_id,),
        ).fetchall()
        if any(r["verified_current_sha256"] is None for r in physical_rows):
            raise ValueError("identity split requires verified current content for every physical File on the source logical Photo")
        attestation_inputs = tuple(
            (r["id"], r["path"], str(r["verified_current_sha256"])) for r in physical_rows
        )
        before_physical = require_expected_physical_bytes(attestation_inputs, context="identity split")
        new_photo_id = str(uuid.uuid4())
        resolution_id = str(uuid.uuid4())
        now = _now()
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?,?)", (new_photo_id, now))
        placeholders = ",".join("?" for _ in file_ids)
        cur = conn.execute(
            f"UPDATE files SET photo_id=? WHERE id IN ({placeholders}) AND photo_id=?",
            (new_photo_id, *file_ids, plan.source_photo_id),
        )
        if cur.rowcount != len(file_ids):
            raise ValueError("identity split lost ownership of one or more selected Files")
        conn.execute(
            "INSERT INTO identity_resolution_history(" 
            "resolution_id,action,library_id,source_photo_id,new_photo_id,sha256,file_ids_json,evidence_fingerprint,note,created_at" 
            ") VALUES (?,'split_hash_cohort',?,?,?,?,?,?,?,?)",
            (resolution_id, plan.library_id, plan.source_photo_id, new_photo_id, plan.sha256,
             json.dumps(list(file_ids), separators=(",", ":")), plan.evidence_fingerprint, note, now),
        )
        after_physical = require_expected_physical_bytes(attestation_inputs, context="identity split")
        if after_physical != before_physical:
            raise ValueError("identity split: physical File changed during execution; run Verify / refresh investigation")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return IdentitySplitResult(resolution_id, plan.source_photo_id, new_photo_id, plan.sha256, file_ids, now)


def list_identity_resolutions(conn: Connection, *, photo_id: str | None = None):
    if photo_id is None:
        return conn.execute("SELECT * FROM identity_resolution_history ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM identity_resolution_history WHERE source_photo_id=? OR new_photo_id=? ORDER BY id",
        (photo_id, photo_id),
    ).fetchall()

IDENTITY_RESOLUTION_REVIEW_SCHEMA = "ppa-identity-resolution-review/3"
IDENTITY_RECOVERY_SCHEMA = "ppa-identity-recovery-plan/3"

@dataclass(frozen=True)
class ResolutionFileState:
    file_id: str
    filename: str
    path: str
    library_id: int
    photo_id: str
    sha256: str | None
    expected_sha256: str | None
    current_revision_id: str | None
    presence_status: str
    health_status: str

@dataclass(frozen=True)
class IdentityResolutionReview:
    schema: str
    resolution_id: str
    library_id: int
    source_photo_id: str
    new_photo_id: str
    split_sha256: str
    moved_file_ids: tuple[str, ...]
    created_at: str
    source_files_now: tuple[ResolutionFileState, ...]
    new_photo_files_now: tuple[ResolutionFileState, ...]
    recovered: bool
    recovery_eligible: bool
    recovery_reason: str

    def to_dict(self) -> dict:
        return {
            "schema": self.schema, "resolution_id": self.resolution_id,
            "library_id": self.library_id, "source_photo_id": self.source_photo_id,
            "new_photo_id": self.new_photo_id, "split_sha256": self.split_sha256,
            "moved_file_ids": list(self.moved_file_ids), "created_at": self.created_at,
            "source_files_now": [f.__dict__ for f in self.source_files_now],
            "new_photo_files_now": [f.__dict__ for f in self.new_photo_files_now],
            "recovered": self.recovered, "recovery_eligible": self.recovery_eligible,
            "recovery_reason": self.recovery_reason,
        }

@dataclass(frozen=True)
class IdentityRecoveryPlan:
    schema: str
    resolution_id: str
    library_id: int
    source_photo_id: str
    new_photo_id: str
    moved_file_ids: tuple[str, ...]
    evidence_fingerprint: str

@dataclass(frozen=True)
class IdentityRecoveryResult:
    recovery_id: str
    resolution_id: str
    source_photo_id: str
    removed_photo_id: str
    moved_file_ids: tuple[str, ...]
    created_at: str


def _resolution_row(conn: Connection, resolution_id: str):
    row = conn.execute("SELECT * FROM identity_resolution_history WHERE resolution_id=?", (resolution_id,)).fetchone()
    if row is None:
        raise ValueError(f"identity resolution not found: {resolution_id}")
    return row


def _parse_file_ids(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise ValueError("identity resolution contains malformed File identity history") from exc
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise ValueError("identity resolution contains malformed File identity history")
    return tuple(value)


def _file_states(conn: Connection, photo_id: str) -> tuple[ResolutionFileState, ...]:
    verified_expr = verified_current_sha256_sql("f", "r")
    rows = conn.execute(
        f"""SELECT f.id,f.filename,f.path,f.library_id,f.photo_id,f.sha256 AS expected_sha256,
                   f.current_revision_id,f.presence_status,f.health_status,
                   {verified_expr} AS verified_current_sha256
              FROM files f
              LEFT JOIN file_revisions r ON r.id=f.current_revision_id
             WHERE f.photo_id=? ORDER BY f.library_id,f.filename COLLATE NOCASE,f.id""", (photo_id,),
    ).fetchall()
    return tuple(ResolutionFileState(
        r['id'], r['filename'], r['path'], r['library_id'], r['photo_id'], r['verified_current_sha256'],
        r['expected_sha256'], r['current_revision_id'], r['presence_status'], r['health_status']
    ) for r in rows)


def _later_identity_dependent_change(conn: Connection, *, source_photo_id: str, new_photo_id: str,
                                     resolution_row) -> str | None:
    created = resolution_row['created_at']; rid = resolution_row['resolution_id']
    # Organisation history is stronger than current membership: add-then-remove still counts as independent use.
    row = conn.execute(
        "SELECT 1 FROM organization_history WHERE photo_id IN (?,?) AND created_at>? LIMIT 1",
        (source_photo_id, new_photo_id, created),
    ).fetchone()
    if row is not None:
        return "Album/Tag curation changed one of these logical Photos after the split"
    row = conn.execute(
        "SELECT 1 FROM photo_lineage_history WHERE (parent_photo_id IN (?,?) OR child_photo_id IN (?,?)) "
        "AND created_at>? LIMIT 1",
        (source_photo_id, new_photo_id, source_photo_id, new_photo_id, created),
    ).fetchone()
    if row is not None:
        return "Photo lineage changed one of these logical Photos after the split"
    row = conn.execute(
        "SELECT 1 FROM identity_resolution_history WHERE resolution_id<>? AND created_at>? "
        "AND (source_photo_id IN (?,?) OR new_photo_id IN (?,?)) LIMIT 1",
        (rid, created, source_photo_id, new_photo_id, source_photo_id, new_photo_id),
    ).fetchone()
    if row is not None:
        return "a later identity resolution touched one of these logical Photos"
    return None


def review_identity_resolution(conn: Connection, resolution_id: str) -> IdentityResolutionReview:
    row = _resolution_row(conn, resolution_id)
    moved = _parse_file_ids(row['file_ids_json'])
    source_files = _file_states(conn, row['source_photo_id'])
    new_files = _file_states(conn, row['new_photo_id'])
    recovered = conn.execute(
        "SELECT 1 FROM identity_resolution_recovery_history WHERE resolution_id=?", (resolution_id,)
    ).fetchone() is not None
    eligible = True; reason = "split remains exactly reversible"
    if recovered:
        eligible = False; reason = "this split has already been recombined"
    elif conn.execute("SELECT 1 FROM photos WHERE id=?", (row['source_photo_id'],)).fetchone() is None:
        eligible = False; reason = "source logical Photo no longer exists"
    elif conn.execute("SELECT 1 FROM photos WHERE id=?", (row['new_photo_id'],)).fetchone() is None:
        eligible = False; reason = "split-created logical Photo no longer exists"
    elif not source_files:
        eligible = False; reason = "source logical Photo no longer has physical Files"
    elif {f.file_id for f in new_files} != set(moved):
        eligible = False; reason = "split-created Photo no longer contains exactly the originally moved File cohort"
    elif any(f.library_id != row['library_id'] for f in new_files):
        eligible = False; reason = "one or more moved Files changed Library ownership"
    elif any(f.sha256 is None for f in (*source_files, *new_files)):
        eligible = False; reason = "one or more Files do not have verified current content; resolve integrity/availability first"
    elif any(f.sha256 != row['sha256'] for f in new_files):
        eligible = False; reason = "one or more moved Files changed bytes after the split"
    else:
        later = _later_identity_dependent_change(conn, source_photo_id=row['source_photo_id'],
                                                new_photo_id=row['new_photo_id'], resolution_row=row)
        if later:
            eligible = False; reason = later
        else:
            verified_expr = verified_current_sha256_sql("f", "r")
            competing = conn.execute(
                f"""SELECT 1 FROM files f
                      LEFT JOIN file_revisions r ON r.id=f.current_revision_id
                     WHERE ({verified_expr})=? AND f.photo_id NOT IN (?,?) LIMIT 1""",
                (row['sha256'], row['source_photo_id'], row['new_photo_id']),
            ).fetchone()
            if competing is not None:
                eligible = False; reason = "identical current bytes now belong to another logical Photo"
    return IdentityResolutionReview(
        IDENTITY_RESOLUTION_REVIEW_SCHEMA, row['resolution_id'], row['library_id'], row['source_photo_id'],
        row['new_photo_id'], row['sha256'], moved, row['created_at'], source_files, new_files,
        recovered, eligible, reason,
    )


def plan_identity_recovery(conn: Connection, resolution_id: str) -> IdentityRecoveryPlan:
    review = review_identity_resolution(conn, resolution_id)
    if not review.recovery_eligible:
        raise ValueError(f"identity split cannot be recombined: {review.recovery_reason}")
    payload = {
        "resolution_id": review.resolution_id,
        "source_photo_id": review.source_photo_id,
        "new_photo_id": review.new_photo_id,
        "files": [f.__dict__ for f in (*review.source_files_now, *review.new_photo_files_now)],
    }
    fp = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return IdentityRecoveryPlan(IDENTITY_RECOVERY_SCHEMA, review.resolution_id, review.library_id,
                                review.source_photo_id, review.new_photo_id, review.moved_file_ids, fp)


def execute_identity_recovery(conn: Connection, plan: IdentityRecoveryPlan, *, note: str | None = None) -> IdentityRecoveryResult:
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise ValueError("identity-recovery note is too long")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = plan_identity_recovery(conn, plan.resolution_id)
        except ValueError as exc:
            raise ValueError("identity recovery plan is stale; refresh and review again") from exc
        if fresh.evidence_fingerprint != plan.evidence_fingerprint:
            raise ValueError("identity recovery plan is stale; refresh and review again")
        physical_states = (*_file_states(conn, fresh.source_photo_id), *_file_states(conn, fresh.new_photo_id))
        if any(f.sha256 is None for f in physical_states):
            raise ValueError("identity recovery requires verified current content for every relevant physical File")
        attestation_inputs = tuple((f.file_id, f.path, str(f.sha256)) for f in physical_states)
        before_physical = require_expected_physical_bytes(attestation_inputs, context="identity recovery")
        placeholders = ",".join("?" for _ in fresh.moved_file_ids)
        cur = conn.execute(
            f"UPDATE files SET photo_id=? WHERE id IN ({placeholders}) AND photo_id=?",
            (fresh.source_photo_id, *fresh.moved_file_ids, fresh.new_photo_id),
        )
        if cur.rowcount != len(fresh.moved_file_ids):
            raise ValueError("identity recovery lost ownership of one or more split Files")
        # Nothing identity-dependent is allowed on this Photo when recovery is eligible.
        cur = conn.execute("DELETE FROM photos WHERE id=?", (fresh.new_photo_id,))
        if cur.rowcount != 1:
            raise ValueError("split-created logical Photo could not be retired")
        recovery_id = str(uuid.uuid4()); now = _now()
        conn.execute(
            "INSERT INTO identity_resolution_recovery_history(recovery_id,resolution_id,action,library_id,"
            "source_photo_id,recombined_photo_id,file_ids_json,evidence_fingerprint,note,created_at) "
            "VALUES (?,?,'recombine_split',?,?,?,?,?,?,?)",
            (recovery_id, fresh.resolution_id, fresh.library_id, fresh.source_photo_id, fresh.new_photo_id,
             json.dumps(list(fresh.moved_file_ids), separators=(",", ":")), fresh.evidence_fingerprint, note, now),
        )
        after_physical = require_expected_physical_bytes(attestation_inputs, context="identity recovery")
        if after_physical != before_physical:
            raise ValueError("identity recovery: physical File changed during execution; run Verify / refresh investigation")
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return IdentityRecoveryResult(recovery_id, fresh.resolution_id, fresh.source_photo_id,
                                  fresh.new_photo_id, fresh.moved_file_ids, now)


def list_identity_recoveries(conn: Connection, *, photo_id: str | None = None):
    if photo_id is None:
        return conn.execute("SELECT * FROM identity_resolution_recovery_history ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM identity_resolution_recovery_history WHERE source_photo_id=? OR recombined_photo_id=? ORDER BY id",
        (photo_id, photo_id),
    ).fetchall()
