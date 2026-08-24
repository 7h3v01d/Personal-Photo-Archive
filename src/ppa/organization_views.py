"""Phase 9.7 — durable saved organisation discovery recipes.

Saved organisation views persist selector intent only. They never cache Photo
membership: evaluation always runs against current Album/Tag membership.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection

from ppa.organization_discovery import OrganizationDiscoveryResult, build_organization_discovery


@dataclass(frozen=True)
class SavedOrganizationView:
    id: str
    library_id: int
    name: str
    album_ids: tuple[str, ...]
    tag_ids: tuple[str, ...]
    created_at: str
    updated_at: str

    @property
    def selector_count(self) -> int:
        return len(self.album_ids) + len(self.tag_ids)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name(value: str) -> str:
    out = " ".join(str(value).split())
    if not out:
        raise ValueError("saved organisation view name must not be blank")
    if len(out) > 120:
        raise ValueError("saved organisation view name is too long")
    return out


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(v) for v in values if str(v)))


def _decode_ids(value: str) -> tuple[str, ...]:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("saved organisation view contains invalid selector JSON") from exc
    if not isinstance(data, list) or any(not isinstance(x, str) or not x for x in data):
        raise ValueError("saved organisation view contains invalid selector ids")
    return _unique(data)


def _row(row) -> SavedOrganizationView:
    return SavedOrganizationView(
        row["id"], int(row["library_id"]), row["name"],
        _decode_ids(row["album_ids_json"]), _decode_ids(row["tag_ids_json"]),
        row["created_at"], row["updated_at"],
    )


def _validate_recipe(conn: Connection, library_id: int, album_ids: tuple[str, ...], tag_ids: tuple[str, ...]) -> None:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    if not album_ids and not tag_ids:
        raise ValueError("saved organisation view requires at least one Album or Tag")
    for table, label, ids in (("albums", "Album", album_ids), ("tags", "Tag", tag_ids)):
        if not ids:
            continue
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT id,library_id FROM {table} WHERE id IN ({marks})", ids).fetchall()
        if len(rows) != len(ids):
            raise ValueError(f"unknown {label} in saved organisation view")
        if any(int(r["library_id"]) != int(library_id) for r in rows):
            raise ValueError("saved organisation view cannot cross Libraries")


def save_organization_view(conn: Connection, *, library_id: int, name: str,
                           album_ids=(), tag_ids=()) -> SavedOrganizationView:
    name = _name(name)
    albums = _unique(album_ids); tags = _unique(tag_ids)
    _validate_recipe(conn, library_id, albums, tags)
    existing = conn.execute(
        "SELECT * FROM saved_organization_views WHERE library_id=? AND name=? COLLATE NOCASE",
        (library_id, name),
    ).fetchone()
    now = _now()
    ajson = json.dumps(list(albums), separators=(",", ":"))
    tjson = json.dumps(list(tags), separators=(",", ":"))
    if existing:
        conn.execute(
            "UPDATE saved_organization_views SET name=?,album_ids_json=?,tag_ids_json=?,updated_at=? WHERE id=?",
            (name, ajson, tjson, now, existing["id"]),
        )
        conn.commit()
        return get_organization_view(conn, existing["id"])
    view_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO saved_organization_views(id,library_id,name,album_ids_json,tag_ids_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (view_id, library_id, name, ajson, tjson, now, now),
    )
    conn.commit()
    return get_organization_view(conn, view_id)


def get_organization_view(conn: Connection, view_id: str) -> SavedOrganizationView:
    row = conn.execute("SELECT * FROM saved_organization_views WHERE id=?", (view_id,)).fetchone()
    if row is None:
        raise ValueError(f"saved organisation view not found: {view_id}")
    return _row(row)


def list_organization_views(conn: Connection, *, library_id: int) -> tuple[SavedOrganizationView, ...]:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    rows = conn.execute(
        "SELECT * FROM saved_organization_views WHERE library_id=? ORDER BY name COLLATE NOCASE,id",
        (library_id,),
    ).fetchall()
    return tuple(_row(r) for r in rows)


def delete_organization_view(conn: Connection, view_id: str) -> bool:
    cur = conn.execute("DELETE FROM saved_organization_views WHERE id=?", (view_id,))
    conn.commit()
    return cur.rowcount > 0


def evaluate_organization_view(conn: Connection, view: SavedOrganizationView) -> OrganizationDiscoveryResult:
    current = get_organization_view(conn, view.id)
    if current.library_id != view.library_id:
        raise ValueError("saved organisation view library mismatch")
    return build_organization_discovery(
        conn, library_id=current.library_id,
        album_ids=current.album_ids, tag_ids=current.tag_ids,
    )
