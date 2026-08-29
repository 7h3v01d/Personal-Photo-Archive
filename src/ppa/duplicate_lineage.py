"""Phase 10.0 — exact-copy identity and explicit Photo lineage.

Two concepts are intentionally separate:

* Exact duplicate bytes are multiple physical ``File`` records attached to the
  same logical ``Photo``. They require no lineage edge.
* A lineage edge connects two *different* logical Photos and is human-confirmed
  in this phase. It is interpretation/curation only and never changes source
  files, hashes, metadata observations, chronology, Events, Albums or Tags.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection

from ppa.current_identity import verified_current_sha256_sql
from ppa.physical_observation import require_expected_physical_bytes

DUPLICATE_IDENTITY_SCHEMA = "ppa-duplicate-identity/2"
LINEAGE_SCHEMA = "ppa-photo-lineage/1"
RELATION_TYPES = (
    "derived_copy",
    "edited_variant",
    "resized_variant",
    "format_conversion",
    "crop",
    "unknown_derivative",
)


@dataclass(frozen=True)
class ExactCopy:
    file_id: str
    photo_id: str
    filename: str
    library_id: int
    presence_status: str
    health_status: str
    sha256: str | None
    expected_sha256: str | None
    revision_id: str | None


@dataclass(frozen=True)
class ExactDuplicateSet:
    photo_id: str
    copies: tuple[ExactCopy, ...]

    @property
    def copy_count(self) -> int:
        return len(self.copies)

    @property
    def present_count(self) -> int:
        return sum(c.presence_status == "present" for c in self.copies)


@dataclass(frozen=True)
class IdentityDivergence:
    photo_id: str
    known_hashes: tuple[str, ...]
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateIdentityView:
    schema: str
    library_id: int
    sets: tuple[ExactDuplicateSet, ...]
    divergences: tuple[IdentityDivergence, ...]

    @property
    def duplicate_photos(self) -> int:
        return len({s.photo_id for s in self.sets})

    @property
    def duplicate_files(self) -> int:
        return sum(s.copy_count for s in self.sets)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "library_id": self.library_id,
            "duplicate_photos": self.duplicate_photos,
            "duplicate_files": self.duplicate_files,
            "identity_divergences": [d.__dict__ for d in self.divergences],
            "sets": [
                {
                    "photo_id": s.photo_id,
                    "copy_count": s.copy_count,
                    "present_count": s.present_count,
                    "copies": [c.__dict__ for c in s.copies],
                }
                for s in self.sets
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


@dataclass(frozen=True)
class PhotoLineage:
    id: str
    parent_photo_id: str
    child_photo_id: str
    relation_type: str
    source: str
    note: str | None
    created_at: str


@dataclass(frozen=True)
class PhotoLineageHistory:
    id: int
    lineage_id: str
    action: str
    parent_photo_id: str
    child_photo_id: str
    relation_type: str
    source: str
    note: str | None
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_library(conn: Connection, library_id: int) -> None:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")


def _require_photo(conn: Connection, photo_id: str) -> None:
    if conn.execute("SELECT 1 FROM photos WHERE id=?", (photo_id,)).fetchone() is None:
        raise ValueError(f"unknown photo {photo_id}")


def build_duplicate_identity(conn: Connection, *, library_id: int) -> DuplicateIdentityView:
    """Return verified-current exact-copy clusters and identity divergences.

    ``files.sha256`` is expected/revision authority, not unconditional proof of
    current bytes.  Only Files with a verified-current SHA participate in
    current duplicate/divergence claims.  Missing or unhealthy Files remain
    historical catalogue evidence but are excluded from positive byte-identity
    assertions.
    """
    _require_library(conn, library_id)
    verified_expr = verified_current_sha256_sql("f", "r")
    rows = conn.execute(
        f"""
        SELECT f.id, f.photo_id, f.filename, f.library_id, f.presence_status,
               f.health_status, f.sha256 AS expected_sha256, f.current_revision_id,
               {verified_expr} AS verified_current_sha256
          FROM files f
          LEFT JOIN file_revisions r ON r.id=f.current_revision_id
         WHERE f.library_id=?
         ORDER BY f.photo_id, CASE f.presence_status WHEN 'present' THEN 0 ELSE 1 END,
                  f.filename COLLATE NOCASE, f.id
        """, (library_id,),
    ).fetchall()
    by_photo: dict[str, list] = {}
    for r in rows:
        by_photo.setdefault(r["photo_id"], []).append(r)

    sets: list[ExactDuplicateSet] = []
    divergences: list[IdentityDivergence] = []
    for photo_id, photo_rows in sorted(by_photo.items()):
        known_hashes = sorted({r["verified_current_sha256"] for r in photo_rows
                               if r["verified_current_sha256"]})
        if len(known_hashes) > 1:
            divergences.append(IdentityDivergence(
                photo_id, tuple(known_hashes),
                tuple(sorted(r["id"] for r in photo_rows if r["verified_current_sha256"]))))
        by_hash: dict[str, list] = {}
        for r in photo_rows:
            sha = r["verified_current_sha256"]
            if sha:
                by_hash.setdefault(sha, []).append(r)
        for sha, same_rows in sorted(by_hash.items()):
            if len(same_rows) < 2:
                continue
            copies = tuple(ExactCopy(
                file_id=r["id"], photo_id=r["photo_id"], filename=r["filename"],
                library_id=r["library_id"], presence_status=r["presence_status"],
                health_status=r["health_status"], sha256=r["verified_current_sha256"],
                expected_sha256=r["expected_sha256"], revision_id=r["current_revision_id"],
            ) for r in same_rows)
            sets.append(ExactDuplicateSet(photo_id, copies))
    return DuplicateIdentityView(DUPLICATE_IDENTITY_SCHEMA, library_id, tuple(sets), tuple(divergences))


def _lineage_from_row(row) -> PhotoLineage:
    return PhotoLineage(row["id"], row["parent_photo_id"], row["child_photo_id"],
                        row["relation_type"], row["source"], row["note"], row["created_at"])


def get_lineage(conn: Connection, lineage_id: str) -> PhotoLineage:
    row = conn.execute("SELECT * FROM photo_lineage WHERE id=?", (lineage_id,)).fetchone()
    if row is None:
        raise ValueError(f"lineage relation not found: {lineage_id}")
    return _lineage_from_row(row)


def list_lineage(conn: Connection, *, photo_id: str | None = None) -> tuple[PhotoLineage, ...]:
    if photo_id is None:
        rows = conn.execute(
            "SELECT * FROM photo_lineage ORDER BY created_at,id"
        ).fetchall()
    else:
        _require_photo(conn, photo_id)
        rows = conn.execute(
            "SELECT * FROM photo_lineage WHERE parent_photo_id=? OR child_photo_id=? "
            "ORDER BY created_at,id", (photo_id, photo_id)
        ).fetchall()
    return tuple(_lineage_from_row(r) for r in rows)


def add_lineage(conn: Connection, *, parent_photo_id: str, child_photo_id: str,
                relation_type: str, note: str | None = None) -> PhotoLineage:
    """Record one explicit human-confirmed directed derivative relationship."""
    _require_photo(conn, parent_photo_id); _require_photo(conn, child_photo_id)
    if parent_photo_id == child_photo_id:
        raise ValueError("a logical Photo cannot be its own lineage parent")
    if relation_type not in RELATION_TYPES:
        raise ValueError(f"unsupported lineage relation type: {relation_type}")
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise ValueError("lineage note is too long")

    # Byte-identical Files already share a logical Photo. Distinct Photos with
    # an equal current hash indicate inconsistent catalogue identity; refuse to
    # paper over that invariant with a lineage edge.
    verified_a = verified_current_sha256_sql("a", "ar")
    verified_b = verified_current_sha256_sql("b", "br")
    equal_hash = conn.execute(
        f"""
        SELECT 1
          FROM files a
          LEFT JOIN file_revisions ar ON ar.id=a.current_revision_id
          JOIN files b ON b.photo_id=?
          LEFT JOIN file_revisions br ON br.id=b.current_revision_id
         WHERE a.photo_id=?
           AND ({verified_a}) IS NOT NULL
           AND ({verified_a})=({verified_b})
         LIMIT 1
        """, (child_photo_id, parent_photo_id)
    ).fetchone()
    if equal_hash is not None:
        raise ValueError("byte-identical content must share one logical Photo; lineage refused")

    existing = conn.execute(
        "SELECT * FROM photo_lineage WHERE parent_photo_id=? AND child_photo_id=?",
        (parent_photo_id, child_photo_id),
    ).fetchone()
    if existing is not None:
        if existing["relation_type"] == relation_type and existing["note"] == note:
            return _lineage_from_row(existing)
        raise ValueError("a lineage relation already exists for this parent/child pair")

    lineage_id = str(uuid.uuid4()); now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO photo_lineage(id,parent_photo_id,child_photo_id,relation_type,source,note,created_at) "
            "VALUES (?,?,?,?, 'human', ?,?)",
            (lineage_id, parent_photo_id, child_photo_id, relation_type, note, now),
        )
        conn.execute(
            "INSERT INTO photo_lineage_history(lineage_id,action,parent_photo_id,child_photo_id,relation_type,source,note,created_at) "
            "VALUES (?,'create',?,?,?,'human',?,?)",
            (lineage_id, parent_photo_id, child_photo_id, relation_type, note, now),
        )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_lineage(conn, lineage_id)


def remove_lineage(conn: Connection, lineage_id: str) -> bool:
    row = conn.execute("SELECT * FROM photo_lineage WHERE id=?", (lineage_id,)).fetchone()
    if row is None:
        return False
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO photo_lineage_history(lineage_id,action,parent_photo_id,child_photo_id,relation_type,source,note,created_at) "
            "VALUES (?,'remove',?,?,?,?,?,?)",
            (row["id"], row["parent_photo_id"], row["child_photo_id"], row["relation_type"],
             row["source"], row["note"], now),
        )
        conn.execute("DELETE FROM photo_lineage WHERE id=?", (lineage_id,))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return True


def list_lineage_history(conn: Connection, *, lineage_id: str | None = None) -> tuple[PhotoLineageHistory, ...]:
    if lineage_id is None:
        rows = conn.execute("SELECT * FROM photo_lineage_history ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM photo_lineage_history WHERE lineage_id=? ORDER BY id", (lineage_id,)).fetchall()
    return tuple(PhotoLineageHistory(r["id"], r["lineage_id"], r["action"], r["parent_photo_id"],
                                     r["child_photo_id"], r["relation_type"], r["source"],
                                     r["note"], r["created_at"]) for r in rows)


def validate_exact_copy_pair(conn: Connection, *, library_id: int,
                             file_ids: tuple[str, str] | list[str]) -> tuple[ExactCopy, ExactCopy]:
    """Prove two selected Files are verified-current exact copies.

    Expected revision hashes remain historical/catalogue authority after a
    mismatch and therefore cannot satisfy this validator.
    """
    ids = tuple(file_ids)
    if len(ids) != 2 or ids[0] == ids[1]:
        raise ValueError("select exactly two distinct physical Files")
    _require_library(conn, library_id)
    verified_expr = verified_current_sha256_sql("f", "r")
    rows = conn.execute(
        f"""
        SELECT f.id,f.photo_id,f.filename,f.path,f.library_id,f.presence_status,f.health_status,
               f.sha256 AS expected_sha256,f.current_revision_id,
               {verified_expr} AS verified_current_sha256
          FROM files f
          LEFT JOIN file_revisions r ON r.id=f.current_revision_id
         WHERE f.id IN (?,?)
         ORDER BY f.id
        """, ids,
    ).fetchall()
    if len(rows) != 2:
        raise ValueError("one or both selected Files no longer exist")
    if any(r["library_id"] != library_id for r in rows):
        raise ValueError("selected Files must belong to the current Library")
    if rows[0]["photo_id"] != rows[1]["photo_id"]:
        raise ValueError("selected Files do not share one logical Photo")
    sha = rows[0]["verified_current_sha256"]
    if not sha or rows[1]["verified_current_sha256"] != sha:
        raise ValueError("selected Files are not proven current exact copies")
    require_expected_physical_bytes(
        tuple((r["id"], r["path"], str(r["verified_current_sha256"])) for r in rows),
        context="exact-copy validation",
    )
    return tuple(ExactCopy(
        file_id=r["id"], photo_id=r["photo_id"], filename=r["filename"],
        library_id=r["library_id"], presence_status=r["presence_status"],
        health_status=r["health_status"], sha256=r["verified_current_sha256"],
        expected_sha256=r["expected_sha256"], revision_id=r["current_revision_id"],
    ) for r in rows)  # type: ignore[return-value]

