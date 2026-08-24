"""Phase 9.8 — read-only organisation health and curation-gap projection.

Health rows summarise explicit Album/Tag curation state only. They never infer
membership, alter chronology/evidence, or touch source photographs.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sqlite3 import Connection

from ppa.organization_browse import OrganizationBrowseView, build_membership_browse

ORGANIZATION_HEALTH_SCHEMA = "ppa-organization-health/1"


@dataclass(frozen=True)
class OrganizationHealth:
    schema: str
    read_only: bool
    library_id: int
    total_photos: int
    unorganized_photo_ids: tuple[str, ...]
    no_album_photo_ids: tuple[str, ...]
    no_tag_photo_ids: tuple[str, ...]
    empty_album_ids: tuple[str, ...]
    unused_tag_ids: tuple[str, ...]
    albums_with_missing_only_members: tuple[str, ...]
    tags_with_missing_only_members: tuple[str, ...]
    broken_saved_view_ids: tuple[str, ...]

    @property
    def unorganized_count(self) -> int:
        return len(self.unorganized_photo_ids)

    @property
    def no_album_count(self) -> int:
        return len(self.no_album_photo_ids)

    @property
    def no_tag_count(self) -> int:
        return len(self.no_tag_photo_ids)

    @property
    def needs_attention(self) -> bool:
        return any((self.unorganized_photo_ids, self.empty_album_ids, self.unused_tag_ids,
                    self.albums_with_missing_only_members, self.tags_with_missing_only_members,
                    self.broken_saved_view_ids))

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update({
            "unorganized_count": self.unorganized_count,
            "no_album_count": self.no_album_count,
            "no_tag_count": self.no_tag_count,
            "needs_attention": self.needs_attention,
        })
        return data

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


def _decode_selector_ids(raw: str) -> tuple[str, ...] | None:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or any(not isinstance(x, str) or not x for x in data):
        return None
    return tuple(dict.fromkeys(data))


def build_organization_health(conn: Connection, *, library_id: int) -> OrganizationHealth:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    before = conn.total_changes

    photo_ids = tuple(r["photo_id"] for r in conn.execute(
        "SELECT DISTINCT photo_id FROM files WHERE library_id=? ORDER BY photo_id", (library_id,)
    ))
    album_rows = conn.execute(
        "SELECT id FROM albums WHERE library_id=? ORDER BY name COLLATE NOCASE,id", (library_id,)
    ).fetchall()
    tag_rows = conn.execute(
        "SELECT id FROM tags WHERE library_id=? ORDER BY name COLLATE NOCASE,id", (library_id,)
    ).fetchall()
    album_ids = tuple(r["id"] for r in album_rows)
    tag_ids = tuple(r["id"] for r in tag_rows)

    album_members = conn.execute(
        "SELECT ap.album_id,ap.photo_id FROM album_photos ap JOIN albums a ON a.id=ap.album_id "
        "WHERE a.library_id=? ORDER BY ap.album_id,ap.photo_id", (library_id,)
    ).fetchall()
    tag_members = conn.execute(
        "SELECT pt.tag_id,pt.photo_id FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id "
        "WHERE t.library_id=? ORDER BY pt.tag_id,pt.photo_id", (library_id,)
    ).fetchall()

    photos_in_albums = {r["photo_id"] for r in album_members}
    photos_with_tags = {r["photo_id"] for r in tag_members}
    all_photos = set(photo_ids)
    no_album = tuple(sorted(all_photos - photos_in_albums))
    no_tag = tuple(sorted(all_photos - photos_with_tags))
    unorganized = tuple(sorted(all_photos - (photos_in_albums | photos_with_tags)))

    album_member_ids = {r["album_id"] for r in album_members}
    tag_member_ids = {r["tag_id"] for r in tag_members}
    empty_albums = tuple(aid for aid in album_ids if aid not in album_member_ids)
    unused_tags = tuple(tid for tid in tag_ids if tid not in tag_member_ids)

    missing_only_photos = {r["photo_id"] for r in conn.execute(
        "SELECT photo_id FROM files WHERE library_id=? GROUP BY photo_id "
        "HAVING SUM(CASE WHEN presence_status='present' THEN 1 ELSE 0 END)=0 ORDER BY photo_id",
        (library_id,),
    )}
    albums_with_missing = {r["album_id"] for r in album_members if r["photo_id"] in missing_only_photos}
    tags_with_missing = {r["tag_id"] for r in tag_members if r["photo_id"] in missing_only_photos}
    albums_missing = tuple(aid for aid in album_ids if aid in albums_with_missing)
    tags_missing = tuple(tid for tid in tag_ids if tid in tags_with_missing)

    known_albums = set(album_ids); known_tags = set(tag_ids)
    broken_views: list[str] = []
    for row in conn.execute(
        "SELECT id,album_ids_json,tag_ids_json FROM saved_organization_views "
        "WHERE library_id=? ORDER BY name COLLATE NOCASE,id", (library_id,)
    ):
        albums = _decode_selector_ids(row["album_ids_json"])
        tags = _decode_selector_ids(row["tag_ids_json"])
        if albums is None or tags is None or (not albums and not tags):
            broken_views.append(row["id"]); continue
        if any(a not in known_albums for a in albums) or any(t not in known_tags for t in tags):
            broken_views.append(row["id"])

    if conn.total_changes != before:
        raise RuntimeError("organisation health projection must be read-only")
    return OrganizationHealth(
        ORGANIZATION_HEALTH_SCHEMA, True, library_id, len(photo_ids),
        unorganized, no_album, no_tag, empty_albums, unused_tags,
        albums_missing, tags_missing, tuple(broken_views),
    )


def build_gap_browse(conn: Connection, health: OrganizationHealth, gap: str) -> OrganizationBrowseView:
    mapping = {
        "unorganized": (health.unorganized_photo_ids, "Unorganised Photos"),
        "no_album": (health.no_album_photo_ids, "Photos with no Album"),
        "no_tag": (health.no_tag_photo_ids, "Photos with no Tags"),
    }
    if gap not in mapping:
        raise ValueError("gap must be 'unorganized', 'no_album', or 'no_tag'")
    ids, name = mapping[gap]
    return build_membership_browse(
        conn, library_id=health.library_id, photo_ids=ids,
        object_kind="organization_gap", object_id=gap, name=name,
        description="Read-only organisation curation gap",
    )


def concise_text(health: OrganizationHealth) -> str:
    return "\n".join([
        "PPA Organisation Health",
        "=======================",
        f"Library: {health.library_id}",
        f"Logical photos: {health.total_photos}",
        f"Unorganised: {health.unorganized_count}",
        f"No Album: {health.no_album_count}",
        f"No Tags: {health.no_tag_count}",
        f"Empty Albums: {len(health.empty_album_ids)}",
        f"Unused Tags: {len(health.unused_tag_ids)}",
        f"Albums with missing-only members: {len(health.albums_with_missing_only_members)}",
        f"Tags with missing-only members: {len(health.tags_with_missing_only_members)}",
        f"Broken saved views: {len(health.broken_saved_view_ids)}",
        f"Needs attention: {'yes' if health.needs_attention else 'no'}",
    ])
