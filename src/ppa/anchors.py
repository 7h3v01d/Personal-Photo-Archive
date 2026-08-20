"""User anchors — human-asserted calendar evidence about when photos were taken.

Anchors are interpretation, not observation. They live in their own table and are
resolved to photos at read time (a join on file id / directory / library); a
photo's bytes and observations are never touched. Resolution is most-specific
first: a file anchor beats a directory anchor beats a library anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from sqlite3 import Connection


@dataclass(frozen=True)
class Anchor:
    id: int
    scope: str            # 'file' | 'directory' | 'library'
    scope_ref: str
    kind: str             # 'exact' | 'range'
    start_date: date
    end_date: date | None
    note: str | None


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def add_anchor(conn: Connection, scope: str, scope_ref: str, kind: str,
               start_date: str, end_date: str | None = None,
               note: str | None = None) -> int:
    """Record a user anchor. Dates are YYYY-MM-DD. Validated by table CHECKs and
    here (so bad input fails before insertion)."""
    if scope not in ("file", "directory", "library"):
        raise ValueError(f"invalid scope: {scope}")
    if kind == "exact" and end_date is not None:
        raise ValueError("exact anchor must not have end_date")
    if kind == "range" and end_date is None:
        raise ValueError("range anchor requires end_date")
    _parse_date(start_date)
    if end_date is not None:
        if _parse_date(end_date) < _parse_date(start_date):
            raise ValueError("end_date must be >= start_date")
    cur = conn.execute(
        "INSERT INTO anchors (scope, scope_ref, kind, start_date, end_date, note, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope, scope_ref, kind, start_date, end_date, note,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def list_anchors(conn: Connection) -> list[Anchor]:
    rows = conn.execute(
        "SELECT id, scope, scope_ref, kind, start_date, end_date, note FROM anchors "
        "ORDER BY id"
    ).fetchall()
    return [
        Anchor(r["id"], r["scope"], r["scope_ref"], r["kind"],
               _parse_date(r["start_date"]),
               _parse_date(r["end_date"]) if r["end_date"] else None, r["note"])
        for r in rows
    ]


def resolve_for(anchors: list[Anchor], *, file_id: str, directory: str,
                library_id) -> Anchor | None:
    """Most-specific applicable anchor for a photo: file > directory > library."""
    for scope, ref in (("file", file_id), ("directory", directory),
                       ("library", str(library_id))):
        matches = [a for a in anchors if a.scope == scope and a.scope_ref == ref]
        if matches:
            return matches[-1]     # latest wins at a given scope
    return None
