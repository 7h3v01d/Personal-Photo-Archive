"""Phase 9.4 — read-only visual Album library projection.

Album Home is a presentation/discovery view over durable logical-Photo Album
membership. It never changes Album membership, chronology, evidence, metadata,
Events, or source photographs.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sqlite3 import Connection

ALBUM_HOME_SCHEMA = "ppa-album-home/1"


@dataclass(frozen=True)
class AlbumHomeCard:
    album_id: str
    name: str
    description: str | None
    photo_count: int
    present_count: int
    missing_only_count: int
    cover_photo_id: str | None
    cover_file_id: str | None
    cover_path: str | None
    cover_sha256: str | None
    cover_rule: str
    has_custom_cover: bool
    has_custom_order: bool
    search_text: str


@dataclass(frozen=True)
class AlbumHomeView:
    schema: str
    read_only: bool
    library_id: int
    cards: tuple[AlbumHomeCard, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))

    def filtered(self, query: str = "") -> tuple[AlbumHomeCard, ...]:
        terms = tuple(t.casefold() for t in str(query).split() if t.strip())
        if not terms:
            return self.cards
        return tuple(c for c in self.cards if all(t in c.search_text for t in terms))

    def card(self, album_id: str) -> AlbumHomeCard:
        for card in self.cards:
            if card.album_id == album_id:
                return card
        raise ValueError(f"album not present in Album Home: {album_id}")


def _require_library(conn: Connection, library_id: int) -> None:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")


def build_album_home(conn: Connection, *, library_id: int) -> AlbumHomeView:
    """Build one deterministic Album-card index using bounded library-wide SQL."""
    _require_library(conn, library_id)
    before = conn.total_changes

    albums = conn.execute(
        "SELECT id,name,description FROM albums WHERE library_id=? "
        "ORDER BY name COLLATE NOCASE,id", (library_id,)
    ).fetchall()
    if not albums:
        return AlbumHomeView(ALBUM_HOME_SCHEMA, True, library_id, ())

    album_ids = tuple(r["id"] for r in albums)
    marks = ",".join("?" for _ in album_ids)

    member_rows = conn.execute(
        "SELECT ap.album_id,ap.photo_id FROM album_photos ap "
        "WHERE ap.album_id IN (" + marks + ") ORDER BY ap.album_id,ap.photo_id",
        album_ids,
    ).fetchall()
    presentation_rows = conn.execute(
        "SELECT album_id,cover_photo_id,order_json FROM album_presentation "
        "WHERE album_id IN (" + marks + ")", album_ids,
    ).fetchall()

    members: dict[str, list[str]] = {aid: [] for aid in album_ids}
    all_photo_ids: set[str] = set()
    for row in member_rows:
        members[row["album_id"]].append(row["photo_id"])
        all_photo_ids.add(row["photo_id"])
    presentation = {r["album_id"]: r for r in presentation_rows}

    files_by_photo: dict[str, list] = {pid: [] for pid in all_photo_ids}
    if all_photo_ids:
        pmarks = ",".join("?" for _ in all_photo_ids)
        file_rows = conn.execute(
            "SELECT id,photo_id,path,filename,sha256,presence_status,status FROM files "
            "WHERE library_id=? AND photo_id IN (" + pmarks + ") "
            "ORDER BY photo_id,CASE WHEN presence_status='present' THEN 0 ELSE 1 END,"
            "filename COLLATE NOCASE,id",
            (library_id, *sorted(all_photo_ids)),
        ).fetchall()
        for row in file_rows:
            files_by_photo.setdefault(row["photo_id"], []).append(row)

    cards: list[AlbumHomeCard] = []
    for album in albums:
        aid = album["id"]
        pids = tuple(members.get(aid, ()))
        present_count = 0
        missing_only_count = 0
        for pid in pids:
            copies = files_by_photo.get(pid, ())
            if not copies:
                raise ValueError("album member is not represented in its library")
            if any(r["presence_status"] == "present" for r in copies):
                present_count += 1
            else:
                missing_only_count += 1

        pref = presentation.get(aid)
        preferred = pref["cover_photo_id"] if pref and pref["cover_photo_id"] in set(pids) else None
        if preferred is not None:
            cover_pid = preferred
            cover_rule = "human_preferred_member"
        elif pids:
            cover_pid = min(pids)
            cover_rule = "stable_logical_photo_id"
        else:
            cover_pid = None
            cover_rule = "empty_album"

        cover_file_id = cover_path = cover_sha = None
        if cover_pid is not None:
            copies = files_by_photo.get(cover_pid, ())
            if not copies:
                raise ValueError("album cover member is not represented in its library")
            rep = copies[0]
            cover_file_id = rep["id"]
            cover_path = rep["path"]
            cover_sha = rep["sha256"]

        description = album["description"]
        search_text = " ".join(x for x in (album["name"], description or "") if x).casefold()
        cards.append(AlbumHomeCard(
            aid, album["name"], description, len(pids), present_count, missing_only_count,
            cover_pid, cover_file_id, cover_path, cover_sha, cover_rule,
            preferred is not None,
            bool(pref and pref["order_json"]),
            search_text,
        ))

    if conn.total_changes != before:
        raise RuntimeError("Album Home projection must be read-only")
    return AlbumHomeView(ALBUM_HOME_SCHEMA, True, library_id, tuple(cards))


def concise_text(home: AlbumHomeView) -> str:
    lines = ["PPA Albums", "==========", f"Library: {home.library_id}", f"Albums: {len(home.cards)}"]
    for c in home.cards:
        suffix = f" — {c.description}" if c.description else ""
        lines.append(f"{c.name} ({c.photo_count} photos; {c.missing_only_count} missing-only){suffix}")
    return "\n".join(lines)
