"""Phase 9.0 — durable, audited human Albums and Tags.

Albums and Tags organise logical Photo identities. They are deliberately
orthogonal to chronology: this module never reads or writes date evidence,
reconstructions, metadata observations, anchors, or source files.
"""
from __future__ import annotations

import uuid
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection


@dataclass(frozen=True)
class Album:
    id: str
    library_id: int
    name: str
    description: str | None
    created_at: str
    updated_at: str
    photo_ids: tuple[str, ...]


@dataclass(frozen=True)
class Tag:
    id: str
    library_id: int
    name: str
    created_at: str
    updated_at: str
    photo_ids: tuple[str, ...]


@dataclass(frozen=True)
class OrganizationHistoryEntry:
    id: int
    library_id: int
    object_kind: str
    object_id: str
    action: str
    photo_id: str | None
    old_value: str | None
    new_value: str | None
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_name(value: str, *, kind: str) -> str:
    name = " ".join(str(value).split())
    if not name:
        raise ValueError(f"{kind} name must not be blank")
    if len(name) > 200:
        raise ValueError(f"{kind} name is too long")
    return name


def _clean_description(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 8000:
        raise ValueError("album description is too long")
    return text


def _require_library(conn: Connection, library_id: int) -> None:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")


def _require_photo_in_library(conn: Connection, library_id: int, photo_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM files WHERE library_id=? AND photo_id=? LIMIT 1", (library_id, photo_id)
    ).fetchone() is None:
        raise ValueError("photo is not represented in this library")


def _album_from_row(conn: Connection, row) -> Album:
    ids = tuple(r["photo_id"] for r in conn.execute(
        "SELECT photo_id FROM album_photos WHERE album_id=? ORDER BY photo_id", (row["id"],)
    ))
    return Album(row["id"], row["library_id"], row["name"], row["description"],
                 row["created_at"], row["updated_at"], ids)


def _tag_from_row(conn: Connection, row) -> Tag:
    ids = tuple(r["photo_id"] for r in conn.execute(
        "SELECT photo_id FROM photo_tags WHERE tag_id=? ORDER BY photo_id", (row["id"],)
    ))
    return Tag(row["id"], row["library_id"], row["name"], row["created_at"], row["updated_at"], ids)


def create_album(conn: Connection, *, library_id: int, name: str,
                 description: str | None = None) -> Album:
    _require_library(conn, library_id)
    name = _clean_name(name, kind="album")
    description = _clean_description(description)
    now = _now(); album_id = str(uuid.uuid4())
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO albums(id,library_id,name,description,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                     (album_id, library_id, name, description, now, now))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,new_value,created_at) "
                     "VALUES (?,'album',?,'create',?,?)", (library_id, album_id, name, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album(conn, album_id)


def get_album(conn: Connection, album_id: str) -> Album:
    row = conn.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
    if row is None:
        raise ValueError(f"album not found: {album_id}")
    return _album_from_row(conn, row)


def list_albums(conn: Connection, *, library_id: int) -> tuple[Album, ...]:
    _require_library(conn, library_id)
    rows = conn.execute("SELECT * FROM albums WHERE library_id=? ORDER BY name COLLATE NOCASE,id", (library_id,)).fetchall()
    return tuple(_album_from_row(conn, r) for r in rows)


def rename_album(conn: Connection, album_id: str, name: str) -> Album:
    old = get_album(conn, album_id); name = _clean_name(name, kind="album")
    if name == old.name: return old
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE albums SET name=?,updated_at=? WHERE id=?", (name, now, album_id))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,old_value,new_value,created_at) "
                     "VALUES (?,'album',?,'rename',?,?,?)", (old.library_id, album_id, old.name, name, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album(conn, album_id)


def update_album_description(conn: Connection, album_id: str, description: str | None) -> Album:
    old = get_album(conn, album_id); description = _clean_description(description)
    if description == old.description: return old
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE albums SET description=?,updated_at=? WHERE id=?", (description, now, album_id))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,old_value,new_value,created_at) "
                     "VALUES (?,'album',?,'description',?,?,?)",
                     (old.library_id, album_id, old.description, description, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album(conn, album_id)


def add_photo_to_album(conn: Connection, album_id: str, photo_id: str) -> Album:
    album = get_album(conn, album_id); _require_photo_in_library(conn, album.library_id, photo_id)
    if photo_id in album.photo_ids: return album
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO album_photos(album_id,photo_id,added_at) VALUES (?,?,?)", (album_id, photo_id, now))
        conn.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album_id))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,new_value,created_at) "
                     "VALUES (?,'album',?,'add_photo',?,'member',?)", (album.library_id, album_id, photo_id, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album(conn, album_id)


def remove_photo_from_album(conn: Connection, album_id: str, photo_id: str) -> Album:
    album = get_album(conn, album_id)
    if photo_id not in album.photo_ids: return album
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM album_photos WHERE album_id=? AND photo_id=?", (album_id, photo_id))
        conn.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album_id))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,old_value,created_at) "
                     "VALUES (?,'album',?,'remove_photo',?,'member',?)", (album.library_id, album_id, photo_id, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album(conn, album_id)


def create_tag(conn: Connection, *, library_id: int, name: str) -> Tag:
    _require_library(conn, library_id); name = _clean_name(name, kind="tag")
    existing = conn.execute("SELECT * FROM tags WHERE library_id=? AND name=? COLLATE NOCASE", (library_id, name)).fetchone()
    if existing is not None: return _tag_from_row(conn, existing)
    now = _now(); tag_id = str(uuid.uuid4())
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO tags(id,library_id,name,created_at,updated_at) VALUES (?,?,?,?,?)", (tag_id, library_id, name, now, now))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,new_value,created_at) "
                     "VALUES (?,'tag',?,'create',?,?)", (library_id, tag_id, name, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_tag(conn, tag_id)


def get_tag(conn: Connection, tag_id: str) -> Tag:
    row = conn.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
    if row is None: raise ValueError(f"tag not found: {tag_id}")
    return _tag_from_row(conn, row)


def list_tags(conn: Connection, *, library_id: int) -> tuple[Tag, ...]:
    _require_library(conn, library_id)
    rows = conn.execute("SELECT * FROM tags WHERE library_id=? ORDER BY name COLLATE NOCASE,id", (library_id,)).fetchall()
    return tuple(_tag_from_row(conn, r) for r in rows)


def rename_tag(conn: Connection, tag_id: str, name: str) -> Tag:
    old = get_tag(conn, tag_id); name = _clean_name(name, kind="tag")
    if name.casefold() == old.name.casefold():
        if name == old.name: return old
    other = conn.execute("SELECT id FROM tags WHERE library_id=? AND name=? COLLATE NOCASE AND id<>?", (old.library_id, name, tag_id)).fetchone()
    if other: raise ValueError("tag name already exists in this library")
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE tags SET name=?,updated_at=? WHERE id=?", (name, now, tag_id))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,old_value,new_value,created_at) "
                     "VALUES (?,'tag',?,'rename',?,?,?)", (old.library_id, tag_id, old.name, name, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_tag(conn, tag_id)


def tag_photo(conn: Connection, tag_id: str, photo_id: str) -> Tag:
    tag = get_tag(conn, tag_id); _require_photo_in_library(conn, tag.library_id, photo_id)
    if photo_id in tag.photo_ids: return tag
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO photo_tags(tag_id,photo_id,added_at) VALUES (?,?,?)", (tag_id, photo_id, now))
        conn.execute("UPDATE tags SET updated_at=? WHERE id=?", (now, tag_id))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,new_value,created_at) "
                     "VALUES (?,'tag',?,'add_photo',?,'tagged',?)", (tag.library_id, tag_id, photo_id, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_tag(conn, tag_id)


def untag_photo(conn: Connection, tag_id: str, photo_id: str) -> Tag:
    tag = get_tag(conn, tag_id)
    if photo_id not in tag.photo_ids: return tag
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM photo_tags WHERE tag_id=? AND photo_id=?", (tag_id, photo_id))
        conn.execute("UPDATE tags SET updated_at=? WHERE id=?", (now, tag_id))
        conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,old_value,created_at) "
                     "VALUES (?,'tag',?,'remove_photo',?,'tagged',?)", (tag.library_id, tag_id, photo_id, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_tag(conn, tag_id)


def list_photo_albums(conn: Connection, *, library_id: int, photo_id: str) -> tuple[Album, ...]:
    _require_photo_in_library(conn, library_id, photo_id)
    rows = conn.execute("SELECT a.* FROM albums a JOIN album_photos ap ON ap.album_id=a.id "
                        "WHERE a.library_id=? AND ap.photo_id=? ORDER BY a.name COLLATE NOCASE,a.id", (library_id, photo_id)).fetchall()
    return tuple(_album_from_row(conn, r) for r in rows)


def list_photo_tags(conn: Connection, *, library_id: int, photo_id: str) -> tuple[Tag, ...]:
    _require_photo_in_library(conn, library_id, photo_id)
    rows = conn.execute("SELECT t.* FROM tags t JOIN photo_tags pt ON pt.tag_id=t.id "
                        "WHERE t.library_id=? AND pt.photo_id=? ORDER BY t.name COLLATE NOCASE,t.id", (library_id, photo_id)).fetchall()
    return tuple(_tag_from_row(conn, r) for r in rows)


def list_organization_history(conn: Connection, *, object_kind: str, object_id: str) -> tuple[OrganizationHistoryEntry, ...]:
    if object_kind not in {"album", "tag"}: raise ValueError("unknown organization object kind")
    rows = conn.execute("SELECT * FROM organization_history WHERE object_kind=? AND object_id=? ORDER BY id", (object_kind, object_id)).fetchall()
    return tuple(OrganizationHistoryEntry(r["id"], r["library_id"], r["object_kind"], r["object_id"], r["action"],
                                          r["photo_id"], r["old_value"], r["new_value"], r["created_at"]) for r in rows)

@dataclass(frozen=True)
class PhotoOrganization:
    photo_id: str
    album_ids: tuple[str, ...]
    tag_ids: tuple[str, ...]


def get_photo_organization(conn: Connection, *, library_id: int, photo_id: str) -> PhotoOrganization:
    """Read-only organisation state for one logical Photo in a Library."""
    _require_photo_in_library(conn, library_id, photo_id)
    album_ids = tuple(r["album_id"] for r in conn.execute(
        "SELECT ap.album_id FROM album_photos ap JOIN albums a ON a.id=ap.album_id "
        "WHERE a.library_id=? AND ap.photo_id=? ORDER BY ap.album_id", (library_id, photo_id)
    ))
    tag_ids = tuple(r["tag_id"] for r in conn.execute(
        "SELECT pt.tag_id FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id "
        "WHERE t.library_id=? AND pt.photo_id=? ORDER BY pt.tag_id", (library_id, photo_id)
    ))
    return PhotoOrganization(photo_id, album_ids, tag_ids)


def bulk_add_photos_to_album(conn: Connection, album_id: str, photo_ids) -> Album:
    """Atomically add a set of logical Photos to one Album."""
    album = get_album(conn, album_id)
    ids = tuple(dict.fromkeys(str(p) for p in photo_ids))
    for pid in ids:
        _require_photo_in_library(conn, album.library_id, pid)
    existing = set(album.photo_ids)
    todo = tuple(pid for pid in ids if pid not in existing)
    if not todo:
        return album
    now = _now()
    try:
        conn.execute("BEGIN")
        for pid in todo:
            conn.execute("INSERT INTO album_photos(album_id,photo_id,added_at) VALUES (?,?,?)", (album_id, pid, now))
            conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,new_value,created_at) "
                         "VALUES (?,'album',?,'add_photo',?,'member',?)", (album.library_id, album_id, pid, now))
        conn.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album_id))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album(conn, album_id)


def bulk_remove_photos_from_album(conn: Connection, album_id: str, photo_ids) -> Album:
    """Atomically remove selected logical Photos from one Album."""
    album = get_album(conn, album_id)
    ids = tuple(dict.fromkeys(str(p) for p in photo_ids))
    existing = set(album.photo_ids)
    todo = tuple(pid for pid in ids if pid in existing)
    if not todo:
        return album
    now = _now()
    try:
        conn.execute("BEGIN")
        for pid in todo:
            conn.execute("DELETE FROM album_photos WHERE album_id=? AND photo_id=?", (album_id, pid))
            conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,old_value,created_at) "
                         "VALUES (?,'album',?,'remove_photo',?,'member',?)", (album.library_id, album_id, pid, now))
        conn.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album_id))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album(conn, album_id)


def bulk_tag_photos(conn: Connection, tag_id: str, photo_ids, *, _manage_transaction: bool = True) -> Tag:
    """Atomically apply one Tag to selected logical Photos.

    ``_manage_transaction=False`` is an internal composition hook used when a
    higher-level operation must include this audited membership change in a
    larger atomic transaction. Public callers should keep the default.
    """
    tag = get_tag(conn, tag_id)
    ids = tuple(dict.fromkeys(str(p) for p in photo_ids))
    for pid in ids:
        _require_photo_in_library(conn, tag.library_id, pid)
    existing = set(tag.photo_ids)
    todo = tuple(pid for pid in ids if pid not in existing)
    if not todo:
        return tag
    now = _now()
    try:
        if _manage_transaction:
            conn.execute("BEGIN")
        for pid in todo:
            conn.execute("INSERT INTO photo_tags(tag_id,photo_id,added_at) VALUES (?,?,?)", (tag_id, pid, now))
            conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,new_value,created_at) "
                         "VALUES (?,'tag',?,'add_photo',?,'tagged',?)", (tag.library_id, tag_id, pid, now))
        conn.execute("UPDATE tags SET updated_at=? WHERE id=?", (now, tag_id))
        if _manage_transaction:
            conn.commit()
    except Exception:
        if _manage_transaction:
            conn.rollback()
        raise
    return get_tag(conn, tag_id)


def bulk_untag_photos(conn: Connection, tag_id: str, photo_ids) -> Tag:
    """Atomically remove one Tag from selected logical Photos."""
    tag = get_tag(conn, tag_id)
    ids = tuple(dict.fromkeys(str(p) for p in photo_ids))
    existing = set(tag.photo_ids)
    todo = tuple(pid for pid in ids if pid in existing)
    if not todo:
        return tag
    now = _now()
    try:
        conn.execute("BEGIN")
        for pid in todo:
            conn.execute("DELETE FROM photo_tags WHERE tag_id=? AND photo_id=?", (tag_id, pid))
            conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,old_value,created_at) "
                         "VALUES (?,'tag',?,'remove_photo',?,'tagged',?)", (tag.library_id, tag_id, pid, now))
        conn.execute("UPDATE tags SET updated_at=? WHERE id=?", (now, tag_id))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_tag(conn, tag_id)


# Phase 9.3 — display-only Album presentation over logical Photo identity.
@dataclass(frozen=True)
class AlbumPresentation:
    album_id: str
    cover_photo_id: str | None
    order_photo_ids: tuple[str, ...] | None
    updated_at: str | None

@dataclass(frozen=True)
class AlbumPresentationHistoryEntry:
    id: int
    album_id: str
    action: str
    old_value: str | None
    new_value: str | None
    created_at: str


def get_album_presentation(conn: Connection, album_id: str) -> AlbumPresentation:
    album = get_album(conn, album_id)
    row = conn.execute("SELECT * FROM album_presentation WHERE album_id=?", (album_id,)).fetchone()
    if row is None:
        return AlbumPresentation(album_id, None, None, None)
    current = set(album.photo_ids)
    cover = row["cover_photo_id"] if row["cover_photo_id"] in current else None
    order = None
    if row["order_json"]:
        try:
            vals = tuple(json.loads(row["order_json"]))
            if len(vals) == len(album.photo_ids) and len(vals) == len(set(vals)) and set(vals) == current:
                order = vals
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return AlbumPresentation(album_id, cover, order, row["updated_at"])


def _album_presentation_json(p: AlbumPresentation) -> str:
    return json.dumps({"cover_photo_id": p.cover_photo_id,
                       "order_photo_ids": list(p.order_photo_ids) if p.order_photo_ids is not None else None},
                      sort_keys=True, separators=(",", ":"))


def set_album_cover(conn: Connection, album_id: str, photo_id: str | None) -> AlbumPresentation:
    album = get_album(conn, album_id)
    if photo_id is not None and photo_id not in set(album.photo_ids):
        raise ValueError("preferred cover must be a current album member")
    old = get_album_presentation(conn, album_id)
    if old.cover_photo_id == photo_id:
        return old
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO album_presentation(album_id,cover_photo_id,order_json,updated_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(album_id) DO UPDATE SET cover_photo_id=excluded.cover_photo_id,updated_at=excluded.updated_at",
                     (album_id, photo_id, json.dumps(list(old.order_photo_ids)) if old.order_photo_ids is not None else None, now))
        new = AlbumPresentation(album_id, photo_id, old.order_photo_ids, now)
        conn.execute("INSERT INTO album_presentation_history(album_id,action,old_value,new_value,created_at) VALUES (?,'cover',?,?,?)",
                     (album_id, _album_presentation_json(old), _album_presentation_json(new), now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album_presentation(conn, album_id)


def set_album_presentation_order(conn: Connection, album_id: str, photo_ids) -> AlbumPresentation:
    album = get_album(conn, album_id)
    vals = tuple(str(x) for x in photo_ids)
    if len(vals) != len(set(vals)):
        raise ValueError("presentation order contains duplicate members")
    if len(vals) != len(album.photo_ids) or set(vals) != set(album.photo_ids):
        raise ValueError("presentation order must contain every current album member exactly once")
    old = get_album_presentation(conn, album_id)
    if old.order_photo_ids == vals:
        return old
    now = _now(); payload = json.dumps(list(vals), separators=(",", ":"))
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO album_presentation(album_id,cover_photo_id,order_json,updated_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(album_id) DO UPDATE SET order_json=excluded.order_json,updated_at=excluded.updated_at",
                     (album_id, old.cover_photo_id, payload, now))
        new = AlbumPresentation(album_id, old.cover_photo_id, vals, now)
        conn.execute("INSERT INTO album_presentation_history(album_id,action,old_value,new_value,created_at) VALUES (?,'order',?,?,?)",
                     (album_id, _album_presentation_json(old), _album_presentation_json(new), now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album_presentation(conn, album_id)


def reset_album_presentation(conn: Connection, album_id: str) -> AlbumPresentation:
    get_album(conn, album_id)
    old = get_album_presentation(conn, album_id)
    if old.cover_photo_id is None and old.order_photo_ids is None:
        return old
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO album_presentation(album_id,cover_photo_id,order_json,updated_at) VALUES (?,NULL,NULL,?) "
                     "ON CONFLICT(album_id) DO UPDATE SET cover_photo_id=NULL,order_json=NULL,updated_at=excluded.updated_at",
                     (album_id, now))
        new = AlbumPresentation(album_id, None, None, now)
        conn.execute("INSERT INTO album_presentation_history(album_id,action,old_value,new_value,created_at) VALUES (?,'reset',?,?,?)",
                     (album_id, _album_presentation_json(old), _album_presentation_json(new), now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_album_presentation(conn, album_id)


def list_album_presentation_history(conn: Connection, album_id: str) -> tuple[AlbumPresentationHistoryEntry, ...]:
    get_album(conn, album_id)
    rows = conn.execute("SELECT * FROM album_presentation_history WHERE album_id=? ORDER BY id", (album_id,)).fetchall()
    return tuple(AlbumPresentationHistoryEntry(r["id"], r["album_id"], r["action"], r["old_value"], r["new_value"], r["created_at"]) for r in rows)
