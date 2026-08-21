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
    library_id: int | None   # durable owning library (namespace), if known


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def add_anchor(conn: Connection, scope: str, scope_ref: str, kind: str,
               start_date: str, end_date: str | None = None,
               note: str | None = None, library_id: int | None = None) -> int:
    """Record a user anchor. Dates are YYYY-MM-DD.

    ``library_id`` is the owning library. It is required so the anchor has a
    durable namespace (removal can clean it up; resolution won't cross to a
    reused id). For 'library' scope it defaults to the referenced library id.
    """
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

    # Ownership is mandatory and fail-closed: authoritative human evidence may
    # never be recorded for a resource the catalogue cannot identify, and must
    # never be ownerless (an ownerless anchor would apply to every library).
    if scope == "library":
        owner = int(scope_ref)
        if conn.execute("SELECT 1 FROM libraries WHERE id = ?", (owner,)).fetchone() is None:
            raise ValueError(f"library anchor references unknown library {owner}")
        library_id = owner
    elif scope == "file":
        row = conn.execute("SELECT library_id FROM files WHERE id = ?",
                           (scope_ref,)).fetchone()
        if row is None:
            raise ValueError(f"file anchor references unknown file {scope_ref!r}")
        library_id = row["library_id"]
    else:  # directory
        if library_id is None:
            raise ValueError("directory anchor requires an owning library_id")
        if conn.execute("SELECT 1 FROM libraries WHERE id = ?",
                        (library_id,)).fetchone() is None:
            raise ValueError(f"directory anchor references unknown library {library_id}")

    cur = conn.execute(
        "INSERT INTO anchors (scope, scope_ref, kind, start_date, end_date, note, "
        "created_at, library_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (scope, scope_ref, kind, start_date, end_date, note,
         datetime.now(timezone.utc).isoformat(), library_id),
    )
    conn.commit()
    return cur.lastrowid


def list_anchors(conn: Connection) -> list[Anchor]:
    rows = conn.execute(
        "SELECT id, scope, scope_ref, kind, start_date, end_date, note, library_id "
        "FROM anchors ORDER BY id"
    ).fetchall()
    return [
        Anchor(r["id"], r["scope"], r["scope_ref"], r["kind"],
               _parse_date(r["start_date"]),
               _parse_date(r["end_date"]) if r["end_date"] else None, r["note"],
               r["library_id"])
        for r in rows
    ]


def resolve_for(anchors: list[Anchor], *, file_id: str, directory: str,
                library_id) -> Anchor | None:
    """Most-specific applicable anchor for a photo: file > directory > library.

    Fail-closed on ownership: an anchor applies ONLY within its owning library.
    An anchor with no recorded owner (a legacy row from before ownership was
    enforced) is NOT resolved automatically — missing provenance is not global
    provenance; it is retained for audit but stays dormant until reassigned.
    """
    def owns(a: Anchor) -> bool:
        return a.library_id is not None and a.library_id == library_id

    for scope, ref in (("file", file_id), ("directory", directory),
                       ("library", str(library_id))):
        matches = [a for a in anchors
                   if a.scope == scope and a.scope_ref == ref and owns(a)]
        if matches:
            return matches[-1]     # latest wins at a given scope
    return None
