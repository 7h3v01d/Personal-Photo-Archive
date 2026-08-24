"""Phase 9.5 — read-only Tag Home and explicit tag intersections."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sqlite3 import Connection

from ppa.organization_browse import OrganizationBrowseView, build_tag_intersection

TAG_HOME_SCHEMA = "ppa-tag-home/1"
TAG_INTERSECTION_SCHEMA = "ppa-tag-intersection/1"


@dataclass(frozen=True)
class TagHomeCard:
    tag_id: str
    name: str
    photo_count: int
    present_count: int
    missing_only_count: int
    cover_photo_id: str | None
    cover_file_id: str | None
    cover_path: str | None
    cover_sha256: str | None
    search_text: str


@dataclass(frozen=True)
class TagHomeView:
    schema: str
    read_only: bool
    library_id: int
    cards: tuple[TagHomeCard, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))

    def filtered(self, query: str = "") -> tuple[TagHomeCard, ...]:
        terms = tuple(t.casefold() for t in str(query).split() if t.strip())
        if not terms:
            return self.cards
        return tuple(c for c in self.cards if all(t in c.search_text for t in terms))


def _require_library(conn: Connection, library_id: int) -> None:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")


def build_tag_home(conn: Connection, *, library_id: int) -> TagHomeView:
    """Build deterministic visual cards for every Tag in one Library."""
    _require_library(conn, library_id)
    before = conn.total_changes
    tags = conn.execute(
        "SELECT id,name FROM tags WHERE library_id=? ORDER BY name COLLATE NOCASE,id",
        (library_id,),
    ).fetchall()
    if not tags:
        return TagHomeView(TAG_HOME_SCHEMA, True, library_id, ())

    tag_ids = tuple(r["id"] for r in tags)
    marks = ",".join("?" for _ in tag_ids)
    membership = conn.execute(
        "SELECT tag_id,photo_id FROM photo_tags WHERE tag_id IN (" + marks + ") "
        "ORDER BY tag_id,photo_id", tag_ids,
    ).fetchall()
    by_tag: dict[str, list[str]] = {tid: [] for tid in tag_ids}
    all_pids: set[str] = set()
    for row in membership:
        by_tag[row["tag_id"]].append(row["photo_id"])
        all_pids.add(row["photo_id"])

    files_by_photo: dict[str, list] = {pid: [] for pid in all_pids}
    if all_pids:
        pmarks = ",".join("?" for _ in all_pids)
        rows = conn.execute(
            "SELECT id,photo_id,path,filename,sha256,presence_status FROM files "
            "WHERE library_id=? AND photo_id IN (" + pmarks + ") "
            "ORDER BY photo_id,CASE WHEN presence_status='present' THEN 0 ELSE 1 END,"
            "filename COLLATE NOCASE,id",
            (library_id, *sorted(all_pids)),
        ).fetchall()
        for row in rows:
            files_by_photo.setdefault(row["photo_id"], []).append(row)

    cards: list[TagHomeCard] = []
    for tag in tags:
        pids = tuple(by_tag[tag["id"]])
        present = missing = 0
        for pid in pids:
            copies = files_by_photo.get(pid, ())
            if not copies:
                raise ValueError("tag member is not represented in its library")
            if any(r["presence_status"] == "present" for r in copies):
                present += 1
            else:
                missing += 1
        cover_pid = min(pids) if pids else None
        file_id = path = sha = None
        if cover_pid:
            rep = files_by_photo[cover_pid][0]
            file_id, path, sha = rep["id"], rep["path"], rep["sha256"]
        cards.append(TagHomeCard(
            tag["id"], tag["name"], len(pids), present, missing,
            cover_pid, file_id, path, sha, tag["name"].casefold(),
        ))

    if conn.total_changes != before:
        raise RuntimeError("Tag Home projection must be read-only")
    return TagHomeView(TAG_HOME_SCHEMA, True, library_id, tuple(cards))


def build_tag_intersection_view(conn: Connection, *, library_id: int,
                                tag_ids: tuple[str, ...] | list[str]) -> OrganizationBrowseView:
    return build_tag_intersection(conn, library_id=library_id, tag_ids=tuple(tag_ids))


def concise_text(home: TagHomeView) -> str:
    lines = ["PPA Tags", "========", f"Library: {home.library_id}", f"Tags: {len(home.cards)}"]
    for c in home.cards:
        lines.append(f"{c.name} ({c.photo_count} photos; {c.missing_only_count} missing-only)")
    return "\n".join(lines)
