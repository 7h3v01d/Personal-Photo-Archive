"""Phase 9.11 — human-readable organisation activity and fail-closed membership undo.

This layer reads the append-only organization_history ledger. Automatic undo is
intentionally limited to Album/Tag membership actions whose current state and
latest pair-specific history still prove the exact inverse is safe.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from sqlite3 import Connection

ORGANIZATION_ACTIVITY_SCHEMA = "ppa-organization-activity/1"


@dataclass(frozen=True)
class OrganizationActivityEntry:
    id: int
    library_id: int
    object_kind: str
    object_id: str
    object_name: str
    action: str
    photo_id: str | None
    old_value: str | None
    new_value: str | None
    created_at: str
    summary: str
    undoable: bool
    undo_reason: str | None


@dataclass(frozen=True)
class OrganizationActivityView:
    schema: str
    read_only: bool
    library_id: int
    entries: tuple[OrganizationActivityEntry, ...]

    def to_dict(self) -> dict:
        return {"schema": self.schema, "read_only": self.read_only,
                "library_id": self.library_id,
                "entries": [asdict(e) for e in self.entries]}

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


def _require_library(conn: Connection, library_id: int) -> None:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")


def _name_maps(conn: Connection, library_id: int) -> tuple[dict[str, str], dict[str, str]]:
    albums = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id,name FROM albums WHERE library_id=?", (library_id,))}
    tags = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id,name FROM tags WHERE library_id=?", (library_id,))}
    return albums, tags


def _summary(kind: str, name: str, action: str, photo_id: str | None,
             old_value: str | None, new_value: str | None) -> str:
    label = "Album" if kind == "album" else "Tag"
    short = (photo_id[:12] + "…") if photo_id else ""
    if action == "create": return f"Created {label} '{name}'"
    if action == "rename": return f"Renamed {label} '{old_value or ''}' → '{new_value or name}'"
    if action == "description": return f"Updated description for Album '{name}'"
    if action == "add_photo": return f"Added Photo {short} to {label} '{name}'"
    if action == "remove_photo": return f"Removed Photo {short} from {label} '{name}'"
    if action == "undo_add_photo": return f"Undid add: removed Photo {short} from {label} '{name}'"
    if action == "undo_remove_photo": return f"Undid removal: restored Photo {short} to {label} '{name}'"
    return f"{label} '{name}': {action.replace('_', ' ')}"


def _membership_exists(conn: Connection, kind: str, object_id: str, photo_id: str) -> bool:
    if kind == "album":
        row = conn.execute("SELECT 1 FROM album_photos WHERE album_id=? AND photo_id=?", (object_id, photo_id)).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM photo_tags WHERE tag_id=? AND photo_id=?", (object_id, photo_id)).fetchone()
    return row is not None


def _latest_pair_history_id(conn: Connection, kind: str, object_id: str, photo_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM organization_history WHERE object_kind=? AND object_id=? AND photo_id=? "
        "AND action IN ('add_photo','remove_photo','undo_add_photo','undo_remove_photo') "
        "ORDER BY id DESC LIMIT 1", (kind, object_id, photo_id)).fetchone()
    return None if row is None else int(row["id"])


def _undo_status(conn: Connection, row) -> tuple[bool, str | None]:
    action = row["action"]; pid = row["photo_id"]
    if action not in {"add_photo", "remove_photo"} or not pid:
        return False, "Only direct Album/Tag membership changes are automatically reversible"
    if _latest_pair_history_id(conn, row["object_kind"], row["object_id"], pid) != row["id"]:
        return False, "A later membership change exists for this Photo and object"
    exists = _membership_exists(conn, row["object_kind"], row["object_id"], pid)
    if action == "add_photo" and not exists:
        return False, "Photo is no longer a current member"
    if action == "remove_photo" and exists:
        return False, "Photo is already a current member"
    return True, None


def build_organization_activity(conn: Connection, *, library_id: int,
                                limit: int = 200,
                                object_kind: str | None = None,
                                object_id: str | None = None) -> OrganizationActivityView:
    _require_library(conn, library_id)
    if limit < 1 or limit > 5000: raise ValueError("limit must be between 1 and 5000")
    if object_kind is not None and object_kind not in {"album", "tag"}:
        raise ValueError("object_kind must be album or tag")
    before = conn.total_changes
    sql = "SELECT * FROM organization_history WHERE library_id=?"; args: list[object] = [library_id]
    if object_kind is not None: sql += " AND object_kind=?"; args.append(object_kind)
    if object_id is not None: sql += " AND object_id=?"; args.append(object_id)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = conn.execute(sql, tuple(args)).fetchall()
    albums, tags = _name_maps(conn, library_id)
    entries = []
    for r in rows:
        names = albums if r["object_kind"] == "album" else tags
        name = names.get(r["object_id"], f"[deleted {r['object_kind']}]")
        undoable, reason = _undo_status(conn, r)
        entries.append(OrganizationActivityEntry(
            r["id"], r["library_id"], r["object_kind"], r["object_id"], name,
            r["action"], r["photo_id"], r["old_value"], r["new_value"], r["created_at"],
            _summary(r["object_kind"], name, r["action"], r["photo_id"], r["old_value"], r["new_value"]),
            undoable, reason))
    if conn.total_changes != before:
        raise RuntimeError("organisation activity projection must be read-only")
    return OrganizationActivityView(ORGANIZATION_ACTIVITY_SCHEMA, True, library_id, tuple(entries))


def undo_organization_membership(conn: Connection, *, library_id: int, history_id: int) -> OrganizationActivityEntry:
    """Undo one provably current add/remove membership action atomically."""
    _require_library(conn, library_id)
    row = conn.execute("SELECT * FROM organization_history WHERE id=? AND library_id=?", (history_id, library_id)).fetchone()
    if row is None: raise ValueError("organisation history entry not found in this library")
    ok, reason = _undo_status(conn, row)
    if not ok: raise ValueError(f"organisation action cannot be safely undone: {reason}")
    kind, oid, pid, action = row["object_kind"], row["object_id"], row["photo_id"], row["action"]
    if kind == "album":
        obj = conn.execute("SELECT id FROM albums WHERE id=? AND library_id=?", (oid, library_id)).fetchone()
    else:
        obj = conn.execute("SELECT id FROM tags WHERE id=? AND library_id=?", (oid, library_id)).fetchone()
    if obj is None: raise ValueError("organisation object no longer exists")
    # Logical photo must still be represented in the library before a restoration.
    if action == "remove_photo" and conn.execute(
        "SELECT 1 FROM files WHERE library_id=? AND photo_id=? LIMIT 1", (library_id, pid)).fetchone() is None:
        raise ValueError("photo is no longer represented in this library")
    now = datetime.now(timezone.utc).isoformat()
    inverse = "undo_add_photo" if action == "add_photo" else "undo_remove_photo"
    try:
        conn.execute("BEGIN")
        if kind == "album":
            if action == "add_photo":
                conn.execute("DELETE FROM album_photos WHERE album_id=? AND photo_id=?", (oid, pid))
            else:
                conn.execute("INSERT INTO album_photos(album_id,photo_id,added_at) VALUES (?,?,?)", (oid, pid, now))
            conn.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, oid))
        else:
            if action == "add_photo":
                conn.execute("DELETE FROM photo_tags WHERE tag_id=? AND photo_id=?", (oid, pid))
            else:
                conn.execute("INSERT INTO photo_tags(tag_id,photo_id,added_at) VALUES (?,?,?)", (oid, pid, now))
            conn.execute("UPDATE tags SET updated_at=? WHERE id=?", (now, oid))
        conn.execute(
            "INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,old_value,new_value,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (library_id, kind, oid, inverse, pid, str(history_id), "membership_inverse", now))
        new_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
    except Exception:
        conn.rollback(); raise
    view = build_organization_activity(conn, library_id=library_id, limit=20)
    return next(e for e in view.entries if e.id == new_id)


def concise_text(view: OrganizationActivityView) -> str:
    lines = ["PPA Organisation Activity", "=========================", f"Library: {view.library_id}",
             f"Entries: {len(view.entries)}", ""]
    for e in view.entries:
        suffix = " [undoable]" if e.undoable else ""
        lines.append(f"{e.id:>6}  {e.created_at}  {e.summary}{suffix}")
    return "\n".join(lines)
