"""Phase 9.2 — read-only Album/Tag browsing projections.

Organisation membership belongs to logical Photos.  This module chooses one
stable representative File per logical Photo purely for rendering/Preview.
That choice never changes membership, evidence, chronology, or source files.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from sqlite3 import Connection

from ppa.organization import get_album, get_tag, get_album_presentation

ORGANIZATION_BROWSE_SCHEMA = "ppa-organization-browse/1"


@dataclass(frozen=True)
class OrganizationBrowseItem:
    photo_id: str
    file_id: str
    filename: str
    path: str
    sha256: str | None
    status: str
    width_px: int | None
    height_px: int | None
    size_bytes: int
    copy_count: int
    search_text: str


@dataclass(frozen=True)
class OrganizationBrowseView:
    schema: str
    read_only: bool
    object_kind: str
    object_id: str
    library_id: int
    name: str
    description: str | None
    total_members: int
    present_members: int
    missing_only_members: int
    items: tuple[OrganizationBrowseItem, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def filtered(self, query: str = "") -> tuple[OrganizationBrowseItem, ...]:
        terms = tuple(t.casefold() for t in str(query).split() if t.strip())
        if not terms:
            return self.items
        return tuple(i for i in self.items if all(t in i.search_text for t in terms))


def _object(conn: Connection, kind: str, object_id: str):
    if kind == "album":
        obj = get_album(conn, object_id)
        return obj, obj.photo_ids, obj.description
    if kind == "tag":
        obj = get_tag(conn, object_id)
        return obj, obj.photo_ids, None
    raise ValueError("object_kind must be 'album' or 'tag'")


def build_organization_browse(conn: Connection, *, object_kind: str,
                              object_id: str) -> OrganizationBrowseView:
    obj, photo_ids, description = _object(conn, object_kind, object_id)
    if not photo_ids:
        return OrganizationBrowseView(
            ORGANIZATION_BROWSE_SCHEMA, True, object_kind, object_id,
            obj.library_id, obj.name, description, 0, 0, 0, (),
        )

    marks = ",".join("?" for _ in photo_ids)
    rows = conn.execute(
        "SELECT f.*, "
        "(SELECT COUNT(*) FROM files c WHERE c.photo_id=f.photo_id) AS copy_count "
        "FROM files f WHERE f.library_id=? AND f.photo_id IN (" + marks + ") "
        "ORDER BY f.photo_id, CASE WHEN f.presence_status='present' THEN 0 ELSE 1 END, "
        "f.filename COLLATE NOCASE, f.id",
        (obj.library_id, *photo_ids),
    ).fetchall()

    grouped: dict[str, list] = {pid: [] for pid in photo_ids}
    for row in rows:
        grouped.setdefault(row["photo_id"], []).append(row)

    items: list[OrganizationBrowseItem] = []
    present_members = 0
    missing_only = 0
    for pid in photo_ids:
        copies = grouped.get(pid, [])
        if not copies:
            # Database ownership triggers should make this impossible for current
            # rows; fail closed rather than fabricate a renderable member.
            raise ValueError("organisation member is not represented in its library")
        representative = copies[0]
        if any(r["presence_status"] == "present" for r in copies):
            present_members += 1
        else:
            missing_only += 1
        filenames = " ".join(str(r["filename"]) for r in copies)
        items.append(OrganizationBrowseItem(
            photo_id=pid,
            file_id=representative["id"],
            filename=representative["filename"],
            path=representative["path"],
            sha256=representative["sha256"],
            status=representative["status"],
            width_px=representative["width_px"],
            height_px=representative["height_px"],
            size_bytes=representative["size_bytes"],
            copy_count=representative["copy_count"],
            search_text=(filenames + " " + pid).casefold(),
        ))

    # Album custom order is presentation-only. Tags keep the stable filename order.
    if object_kind == "album":
        presentation = get_album_presentation(conn, object_id)
        if presentation.order_photo_ids is not None:
            rank = {pid: idx for idx, pid in enumerate(presentation.order_photo_ids)}
            items.sort(key=lambda i: rank[i.photo_id])
        else:
            items.sort(key=lambda i: (i.filename.casefold(), i.photo_id, i.file_id))
    else:
        items.sort(key=lambda i: (i.filename.casefold(), i.photo_id, i.file_id))
    return OrganizationBrowseView(
        ORGANIZATION_BROWSE_SCHEMA, True, object_kind, object_id,
        obj.library_id, obj.name, description, len(photo_ids),
        present_members, missing_only, tuple(items),
    )


def build_tag_intersection(conn: Connection, *, library_id: int,
                           tag_ids: tuple[str, ...]) -> OrganizationBrowseView:
    """Return the explicit logical-Photo intersection of two or more Tags."""
    unique = tuple(dict.fromkeys(str(t) for t in tag_ids if str(t)))
    if len(unique) < 2:
        raise ValueError("tag intersection requires at least two distinct Tags")
    marks = ",".join("?" for _ in unique)
    rows = conn.execute(
        "SELECT id,name,library_id FROM tags WHERE id IN (" + marks + ")", unique
    ).fetchall()
    if len(rows) != len(unique):
        raise ValueError("unknown Tag in intersection")
    if any(int(r["library_id"]) != int(library_id) for r in rows):
        raise ValueError("Tag intersection cannot cross Libraries")
    names = {r["id"]: r["name"] for r in rows}
    member_rows = conn.execute(
        "SELECT photo_id FROM photo_tags WHERE tag_id IN (" + marks + ") "
        "GROUP BY photo_id HAVING COUNT(DISTINCT tag_id)=? ORDER BY photo_id",
        (*unique, len(unique)),
    ).fetchall()
    photo_ids = tuple(r["photo_id"] for r in member_rows)
    title = " + ".join(names[t] for t in unique)
    if not photo_ids:
        return OrganizationBrowseView(ORGANIZATION_BROWSE_SCHEMA, True, "tag_intersection",
            "+".join(unique), library_id, title, "Explicit Tag intersection", 0, 0, 0, ())
    pmarks = ",".join("?" for _ in photo_ids)
    file_rows = conn.execute(
        "SELECT f.*, (SELECT COUNT(*) FROM files c WHERE c.photo_id=f.photo_id) AS copy_count "
        "FROM files f WHERE f.library_id=? AND f.photo_id IN (" + pmarks + ") "
        "ORDER BY f.photo_id,CASE WHEN f.presence_status='present' THEN 0 ELSE 1 END,"
        "f.filename COLLATE NOCASE,f.id",
        (library_id, *photo_ids),
    ).fetchall()
    grouped = {pid: [] for pid in photo_ids}
    for row in file_rows:
        grouped.setdefault(row["photo_id"], []).append(row)
    items = []
    present = missing = 0
    for pid in photo_ids:
        copies = grouped.get(pid, [])
        if not copies:
            raise ValueError("tag-intersection member is not represented in its library")
        rep = copies[0]
        if any(r["presence_status"] == "present" for r in copies): present += 1
        else: missing += 1
        filenames = " ".join(str(r["filename"]) for r in copies)
        items.append(OrganizationBrowseItem(pid, rep["id"], rep["filename"], rep["path"],
            rep["sha256"], rep["status"], rep["width_px"], rep["height_px"],
            rep["size_bytes"], rep["copy_count"], (filenames + " " + pid).casefold()))
    items.sort(key=lambda i: (i.filename.casefold(), i.photo_id, i.file_id))
    return OrganizationBrowseView(ORGANIZATION_BROWSE_SCHEMA, True, "tag_intersection",
        "+".join(unique), library_id, title, "Explicit Tag intersection", len(photo_ids),
        present, missing, tuple(items))
