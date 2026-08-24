"""Phase 9.6 — unified explicit Album + Tag discovery.

Discovery is a pure set operation over durable logical-Photo membership.  It
never infers membership and never reads/writes chronology evidence.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sqlite3 import Connection

from ppa.organization_browse import OrganizationBrowseView, build_membership_browse

ORGANIZATION_DISCOVERY_SCHEMA = "ppa-organization-discovery/1"


@dataclass(frozen=True)
class OrganizationDiscoveryQuery:
    library_id: int
    album_ids: tuple[str, ...]
    tag_ids: tuple[str, ...]
    album_names: tuple[str, ...]
    tag_names: tuple[str, ...]
    photo_ids: tuple[str, ...]

    @property
    def label(self) -> str:
        parts = [*(f"Album: {n}" for n in self.album_names), *(f"Tag: {n}" for n in self.tag_names)]
        return " + ".join(parts)


@dataclass(frozen=True)
class OrganizationDiscoveryResult:
    schema: str
    read_only: bool
    query: OrganizationDiscoveryQuery
    view: OrganizationBrowseView

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(v) for v in values if str(v)))


def build_organization_discovery(conn: Connection, *, library_id: int,
                                 album_ids=(), tag_ids=()) -> OrganizationDiscoveryResult:
    albums = _unique(album_ids); tags = _unique(tag_ids)
    if not albums and not tags:
        raise ValueError("organisation discovery requires at least one Album or Tag")
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    before = conn.total_changes

    album_rows = []
    if albums:
        marks = ",".join("?" for _ in albums)
        album_rows = conn.execute(
            "SELECT id,name,library_id FROM albums WHERE id IN (" + marks + ")", albums
        ).fetchall()
        if len(album_rows) != len(albums):
            raise ValueError("unknown Album in organisation discovery")
        if any(int(r["library_id"]) != int(library_id) for r in album_rows):
            raise ValueError("organisation discovery cannot cross Libraries")
    tag_rows = []
    if tags:
        marks = ",".join("?" for _ in tags)
        tag_rows = conn.execute(
            "SELECT id,name,library_id FROM tags WHERE id IN (" + marks + ")", tags
        ).fetchall()
        if len(tag_rows) != len(tags):
            raise ValueError("unknown Tag in organisation discovery")
        if any(int(r["library_id"]) != int(library_id) for r in tag_rows):
            raise ValueError("organisation discovery cannot cross Libraries")

    sets: list[set[str]] = []
    for aid in albums:
        sets.append({r["photo_id"] for r in conn.execute(
            "SELECT photo_id FROM album_photos WHERE album_id=?", (aid,))})
    for tid in tags:
        sets.append({r["photo_id"] for r in conn.execute(
            "SELECT photo_id FROM photo_tags WHERE tag_id=?", (tid,))})
    photo_ids = tuple(sorted(set.intersection(*sets))) if sets else ()
    album_names_by_id = {r["id"]: r["name"] for r in album_rows}
    tag_names_by_id = {r["id"]: r["name"] for r in tag_rows}
    query = OrganizationDiscoveryQuery(
        library_id, albums, tags,
        tuple(album_names_by_id[a] for a in albums),
        tuple(tag_names_by_id[t] for t in tags), photo_ids,
    )
    view = build_membership_browse(
        conn, library_id=library_id, photo_ids=photo_ids,
        object_kind="organization_discovery", object_id="+".join((*albums, *tags)),
        name=query.label, description="Explicit Album/Tag intersection",
    )
    if conn.total_changes != before:
        raise RuntimeError("organisation discovery must be read-only")
    return OrganizationDiscoveryResult(ORGANIZATION_DISCOVERY_SCHEMA, True, query, view)


def concise_text(result: OrganizationDiscoveryResult) -> str:
    return f"{result.query.label}: {result.view.total_members} logical photos"
