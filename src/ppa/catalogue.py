"""Catalogue read model.

The UI never issues raw SQL. It asks this module for typed, read-only views
of the catalogue and gets back plain dataclasses. Keeping the read model
here (with zero Qt) means the queries are unit-testable on their own and the
widgets stay thin — they render dataclasses, nothing more.

Nothing in this module writes to the catalogue or to source files.
"""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

# Named views the grid can show. These are all answerable from the Phase 0-2
# schema; Timeline/Albums/Unplaced arrive with their later phases.
VIEW_ALL = "all"
VIEW_RECENT = "recent"
VIEW_DUPLICATES = "duplicates"
VIEW_MISSING = "missing"

VIEWS = (VIEW_ALL, VIEW_RECENT, VIEW_DUPLICATES, VIEW_MISSING)


@dataclass(frozen=True)
class LibraryStats:
    photos: int
    files: int
    total_bytes: int
    active: int
    missing: int
    duplicate_files: int  # files sharing a Photo with at least one other file
    hash_mismatches: int
    last_library_path: str | None


@dataclass(frozen=True)
class GridItem:
    file_id: str
    photo_id: str
    filename: str
    path: str
    sha256: str | None
    status: str
    width_px: int | None
    height_px: int | None
    size_bytes: int


@dataclass(frozen=True)
class IntegrityEvent:
    event_type: str
    detail: str | None
    occurred_at: str


@dataclass(frozen=True)
class PathHistoryEntry:
    path: str
    observed_at: str


@dataclass(frozen=True)
class FileDetail:
    file_id: str
    photo_id: str
    filename: str
    path: str
    extension: str | None
    status: str
    size_bytes: int
    width_px: int | None
    height_px: int | None
    mime_type: str | None
    sha256: str | None
    fs_mtime: str | None
    first_seen_at: str
    last_seen_at: str
    camera: str | None
    copy_count: int  # how many files share this Photo (1 == unique)
    integrity_events: tuple[IntegrityEvent, ...]
    path_history: tuple[PathHistoryEntry, ...]


def library_stats(conn: Connection) -> LibraryStats:
    photos = conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"]
    files = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    total_bytes = (
        conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM files WHERE status = 'active'"
        ).fetchone()["n"]
    )
    active = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE status = 'active'"
    ).fetchone()["n"]
    missing = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE status = 'missing'"
    ).fetchone()["n"]
    duplicate_files = conn.execute(
        """
        SELECT COUNT(*) AS n FROM files
        WHERE photo_id IN (
            SELECT photo_id FROM files GROUP BY photo_id HAVING COUNT(*) > 1
        )
        """
    ).fetchone()["n"]
    hash_mismatches = conn.execute(
        "SELECT COUNT(*) AS n FROM integrity_events WHERE event_type = 'hash_mismatch'"
    ).fetchone()["n"]
    last = conn.execute(
        "SELECT library_path FROM import_sessions ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    return LibraryStats(
        photos=photos,
        files=files,
        total_bytes=total_bytes,
        active=active,
        missing=missing,
        duplicate_files=duplicate_files,
        hash_mismatches=hash_mismatches,
        last_library_path=last["library_path"] if last else None,
    )


def _grid_item(row) -> GridItem:
    return GridItem(
        file_id=row["id"],
        photo_id=row["photo_id"],
        filename=row["filename"],
        path=row["path"],
        sha256=row["sha256"],
        status=row["status"],
        width_px=row["width_px"],
        height_px=row["height_px"],
        size_bytes=row["size_bytes"],
    )


def grid_items(conn: Connection, view: str = VIEW_ALL, limit: int | None = None) -> list[GridItem]:
    """Return the files to show for a given named view.

    Read-only. Ordering is deterministic so the grid is stable between runs.
    """
    if view not in VIEWS:
        raise ValueError(f"Unknown view: {view!r}")

    if view == VIEW_MISSING:
        sql = "SELECT * FROM files WHERE status = 'missing' ORDER BY filename, id"
    elif view == VIEW_RECENT:
        sql = (
            "SELECT * FROM files WHERE status = 'active' "
            "ORDER BY first_seen_at DESC, filename"
        )
    elif view == VIEW_DUPLICATES:
        # Files whose Photo has more than one file, grouped so copies sit
        # together. Missing copies included — the point of the view is to see
        # the whole cluster before any cleanup.
        sql = (
            "SELECT * FROM files WHERE photo_id IN ("
            "  SELECT photo_id FROM files GROUP BY photo_id HAVING COUNT(*) > 1"
            ") ORDER BY photo_id, filename, id"
        )
    else:  # VIEW_ALL
        sql = "SELECT * FROM files WHERE status = 'active' ORDER BY filename, id"

    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    return [_grid_item(r) for r in conn.execute(sql).fetchall()]


def file_detail(conn: Connection, file_id: str) -> FileDetail | None:
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return None

    camera = None
    if row["camera_id"] is not None:
        cam = conn.execute(
            "SELECT make, model FROM cameras WHERE id = ?", (row["camera_id"],)
        ).fetchone()
        if cam is not None:
            camera = " ".join(p for p in (cam["make"], cam["model"]) if p) or None

    copy_count = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE photo_id = ?", (row["photo_id"],)
    ).fetchone()["n"]

    events = tuple(
        IntegrityEvent(
            event_type=e["event_type"],
            detail=e["detail"],
            occurred_at=e["occurred_at"],
        )
        for e in conn.execute(
            "SELECT event_type, detail, occurred_at FROM integrity_events "
            "WHERE file_id = ? ORDER BY occurred_at DESC, id DESC",
            (file_id,),
        ).fetchall()
    )

    history = tuple(
        PathHistoryEntry(path=h["path"], observed_at=h["observed_at"])
        for h in conn.execute(
            "SELECT path, observed_at FROM file_path_history "
            "WHERE file_id = ? ORDER BY id",
            (file_id,),
        ).fetchall()
    )

    return FileDetail(
        file_id=row["id"],
        photo_id=row["photo_id"],
        filename=row["filename"],
        path=row["path"],
        extension=row["extension"],
        status=row["status"],
        size_bytes=row["size_bytes"],
        width_px=row["width_px"],
        height_px=row["height_px"],
        mime_type=row["mime_type"],
        sha256=row["sha256"],
        fs_mtime=row["fs_mtime"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        camera=camera,
        copy_count=copy_count,
        integrity_events=events,
        path_history=history,
    )
