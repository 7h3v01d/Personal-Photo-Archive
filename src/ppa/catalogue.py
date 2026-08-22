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
    hash_mismatches: int            # files currently in mismatch state
    historical_mismatch_events: int  # all mismatch events ever recorded
    last_library_path: str | None


@dataclass(frozen=True)
class LibraryInfo:
    id: int
    display_path: str        # exactly as the user chose it
    canonical_path: str      # normalised absolute root
    state: str               # 'active' | 'unavailable'
    last_scan_at: str | None
    present: int             # files currently present on disk
    missing: int             # files catalogued but currently missing
    available: bool          # the root is reachable right now


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
    copy_count: int  # files sharing this Photo (1 == unique)


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
    health_status: str
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
    gps: tuple[float, float] | None
    observed_metadata: tuple[tuple[str, str], ...]  # curated (label, value) pairs
    integrity_events: tuple[IntegrityEvent, ...]
    path_history: tuple[PathHistoryEntry, ...]


def observations(conn: Connection, file_id: str) -> dict[str, str]:
    """Return this file's CURRENT-revision metadata observations as a flat
    key -> value map. Observations from superseded revisions are history and
    are not shown here. Marker rows (source 'meta') are excluded.
    """
    rows = conn.execute(
        "SELECT source, key, value FROM metadata_observations "
        "WHERE file_id = ? AND source != 'meta' "
        "AND file_revision_id = (SELECT current_revision_id FROM files WHERE id = ?)",
        (file_id, file_id),
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


# Curated view of the raw observations for the inspector: (raw key, label,
# formatter). Only keys actually present are shown. Everything here is an
# OBSERVED value — the interpreted capture date is a later phase.
def _fmt_fnumber(v: str) -> str:
    return f"f/{v}"


def _fmt_focal(v: str) -> str:
    return f"{v} mm"


def _fmt_exposure(v: str) -> str:
    return f"{v} s"


_CURATED: tuple[tuple[str, str, object], ...] = (
    ("DateTimeOriginal", "capture date (observed)", None),
    ("DateTimeDigitized", "digitised", None),
    ("DateTime", "modified (exif)", None),
    ("Make", "camera make", None),
    ("Model", "camera model", None),
    ("LensModel", "lens", None),
    ("BodySerialNumber", "body serial", None),
    ("FNumber", "aperture", _fmt_fnumber),
    ("ExposureTime", "shutter", _fmt_exposure),
    ("ISOSpeedRatings", "ISO", None),
    ("PhotographicSensitivity", "ISO", None),
    ("FocalLength", "focal length", _fmt_focal),
    ("Software", "software", None),
    ("Orientation", "orientation", None),
)


def curated_metadata(conn: Connection, file_id: str) -> list[tuple[str, str]]:
    """Human-facing (label, value) pairs of observed metadata, ordered and
    formatted for display. GPS is folded into a single lat/lon line.
    """
    obs = observations(conn, file_id)
    out: list[tuple[str, str]] = []
    seen_labels: set[str] = set()
    for key, label, fmt in _CURATED:
        if key in obs and label not in seen_labels:
            value = obs[key]
            out.append((label, fmt(value) if callable(fmt) else value))
            seen_labels.add(label)

    lat = obs.get("GPSLatitudeDecimal")
    lon = obs.get("GPSLongitudeDecimal")
    if lat and lon:
        out.append(("GPS", f"{lat}, {lon}"))

    if "mtime" in obs:
        out.append(("file mtime", obs["mtime"]))
    return out


def library_stats(conn: Connection) -> LibraryStats:
    photos = conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"]
    files = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    total_bytes = (
        conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM files WHERE presence_status = 'present'"
        ).fetchone()["n"]
    )
    active = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE presence_status = 'present'"
    ).fetchone()["n"]
    missing = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE presence_status = 'missing'"
    ).fetchone()["n"]
    # "Duplicate files" for the dashboard means copies that currently exist on
    # disk: photos with more than one PRESENT file. A copy that has since gone
    # missing is history, not a current duplicate, so it is excluded here (the
    # Duplicate *view* still shows historical relationships).
    duplicate_files = conn.execute(
        """
        SELECT COUNT(*) AS n FROM files
        WHERE presence_status = 'present'
          AND photo_id IN (
              SELECT photo_id FROM files
              WHERE presence_status = 'present'
              GROUP BY photo_id HAVING COUNT(*) > 1
          )
        """
    ).fetchone()["n"]
    hash_mismatches = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE health_status = 'hash_mismatch'"
    ).fetchone()["n"]
    historical_mismatch_events = conn.execute(
        "SELECT COUNT(*) AS n FROM integrity_events WHERE event_type = 'hash_mismatch'"
    ).fetchone()["n"]
    # Prefer the authoritative libraries table: the most recently scanned
    # library's canonical (absolute) root. import_sessions.library_path stores
    # whatever spelling was supplied (possibly a relative path), which is not a
    # safe selector to reopen with from a different working directory.
    last = conn.execute(
        "SELECT root_display_path, root_canonical_path FROM libraries "
        "WHERE last_scan_at IS NOT NULL ORDER BY last_scan_at DESC LIMIT 1"
    ).fetchone()
    if last is not None:
        # Prefer the display path when it is absolute; otherwise the canonical
        # (always absolute) root.
        import os as _os
        disp = last["root_display_path"]
        last_library_path = disp if (disp and _os.path.isabs(disp)) else last["root_canonical_path"]
    else:
        last_library_path = None

    return LibraryStats(
        photos=photos,
        files=files,
        total_bytes=total_bytes,
        active=active,
        missing=missing,
        duplicate_files=duplicate_files,
        hash_mismatches=hash_mismatches,
        historical_mismatch_events=historical_mismatch_events,
        last_library_path=last_library_path,
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
        copy_count=row["copy_count"],
    )


def grid_items(conn: Connection, view: str = VIEW_ALL, limit: int | None = None) -> list[GridItem]:
    """Return the files to show for a given named view.

    Read-only. Ordering is deterministic so the grid is stable between runs.
    Each item carries ``copy_count`` (files sharing its Photo) so the grid can
    badge duplicates without a second query.
    """
    if view not in VIEWS:
        raise ValueError(f"Unknown view: {view!r}")

    cte = "WITH counts AS (SELECT photo_id, COUNT(*) AS c FROM files GROUP BY photo_id) "
    select = (
        "SELECT f.*, counts.c AS copy_count FROM files f "
        "JOIN counts ON counts.photo_id = f.photo_id "
    )

    if view == VIEW_MISSING:
        where, order = "WHERE f.presence_status = 'missing' ", "ORDER BY f.filename, f.id"
    elif view == VIEW_RECENT:
        where, order = "WHERE f.presence_status = 'present' ", "ORDER BY f.first_seen_at DESC, f.filename"
    elif view == VIEW_DUPLICATES:
        # Every file whose Photo has more than one file, grouped so copies sit
        # together. Missing copies included — the point is to see the cluster.
        where, order = "WHERE counts.c > 1 ", "ORDER BY f.photo_id, f.filename, f.id"
    else:  # VIEW_ALL
        where, order = "WHERE f.presence_status = 'present' ", "ORDER BY f.filename, f.id"

    sql = cte + select + where + order
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    return [_grid_item(r) for r in conn.execute(sql).fetchall()]


def grid_items_for_files(conn: Connection, file_ids: list[str] | tuple[str, ...]) -> list[GridItem]:
    """Return present GridItems in the caller-supplied file-id order.

    Read-only helper for workflow views such as the date-review queue. Unknown or
    non-present ids are omitted; no broad catalogue query can leak extra files.
    """
    if not file_ids:
        return []
    wanted = list(dict.fromkeys(file_ids))
    marks = ",".join("?" for _ in wanted)
    rows = conn.execute(
        "WITH counts AS (SELECT photo_id, COUNT(*) AS c FROM files GROUP BY photo_id) "
        "SELECT f.*, counts.c AS copy_count FROM files f "
        "JOIN counts ON counts.photo_id=f.photo_id "
        f"WHERE f.presence_status='present' AND f.id IN ({marks})",
        wanted,
    ).fetchall()
    by_id = {r["id"]: _grid_item(r) for r in rows}
    return [by_id[fid] for fid in wanted if fid in by_id]


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

    obs = observations(conn, file_id)
    gps: tuple[float, float] | None = None
    lat_s, lon_s = obs.get("GPSLatitudeDecimal"), obs.get("GPSLongitudeDecimal")
    if lat_s and lon_s:
        try:
            gps = (float(lat_s), float(lon_s))
        except ValueError:
            gps = None

    return FileDetail(
        file_id=row["id"],
        photo_id=row["photo_id"],
        filename=row["filename"],
        path=row["path"],
        extension=row["extension"],
        status=row["status"],
        health_status=row["health_status"],
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
        gps=gps,
        observed_metadata=tuple(curated_metadata(conn, file_id)),
        integrity_events=events,
        path_history=history,
    )


# --- library (resource) management ------------------------------------------

def list_libraries(conn: Connection) -> list["LibraryInfo"]:
    """All catalogued photo-source libraries with live present/missing counts.

    ``available`` reflects whether the root directory is reachable right now, so
    the UI can distinguish an offline drive from a genuinely empty library.
    """
    import os

    rows = conn.execute(
        """
        SELECT l.id, l.root_display_path, l.root_canonical_path, l.state, l.last_scan_at,
          (SELECT COUNT(*) FROM files f
             WHERE f.library_id = l.id AND f.presence_status = 'present') AS present,
          (SELECT COUNT(*) FROM files f
             WHERE f.library_id = l.id AND f.presence_status = 'missing') AS missing
        FROM libraries l
        ORDER BY l.root_display_path
        """
    ).fetchall()
    out: list[LibraryInfo] = []
    for r in rows:
        out.append(LibraryInfo(
            id=r["id"],
            display_path=r["root_display_path"],
            canonical_path=r["root_canonical_path"],
            state=r["state"],
            last_scan_at=r["last_scan_at"],
            present=r["present"],
            missing=r["missing"],
            available=os.path.isdir(r["root_canonical_path"]),
        ))
    return out


def forget_library(conn: Connection, library_id: int) -> int:
    """Remove a library and its catalogue records. Returns the number of File
    records removed.

    SAFETY: this only deletes catalogue rows. It never reads, moves, or deletes
    a single source photograph — the files on disk are untouched. Removing a
    library simply makes the archive stop tracking that folder.

    Deletes are ordered to respect foreign keys (the files↔revisions cycle is
    broken by nulling current_revision_id first), and photos are removed only
    when no File anywhere still references them (duplicates in another library
    keep their Photo alive). Atomic: rolls back on any error.
    """
    file_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM files WHERE library_id = ?", (library_id,)).fetchall()]
    photo_ids = {r["photo_id"] for r in conn.execute(
        "SELECT DISTINCT photo_id FROM files WHERE library_id = ?", (library_id,)).fetchall()}

    try:
        conn.execute("BEGIN")
        # Break the files <-> file_revisions reference cycle.
        conn.execute("UPDATE files SET current_revision_id = NULL WHERE library_id = ?",
                     (library_id,))
        if file_ids:
            marks = ",".join("?" * len(file_ids))
            # Observations reference file_revisions; remove them before revisions.
            conn.execute(f"DELETE FROM metadata_observations WHERE file_id IN ({marks})",
                         file_ids)
            conn.execute(f"DELETE FROM file_path_history WHERE file_id IN ({marks})",
                         file_ids)
            conn.execute(f"DELETE FROM integrity_events WHERE file_id IN ({marks})",
                         file_ids)
            # Reconstructions reference file_revisions (source_revision_id); remove
            # them before the revisions they point at.
            conn.execute(f"DELETE FROM reconstructions WHERE file_id IN ({marks})",
                         file_ids)
            conn.execute(f"DELETE FROM file_revisions WHERE file_id IN ({marks})",
                         file_ids)
        conn.execute("DELETE FROM files WHERE library_id = ?", (library_id,))
        # Remove anchors OWNED by this library, so authoritative human date
        # evidence never outlives the resource or leaks onto a reused library id.
        # Covered three ways: the library-scoped anchor by its ref, file-scoped
        # anchors for the files just removed, and anything carrying this
        # library_id (directory anchors, plus any of the above with ownership).
        conn.execute("DELETE FROM anchors WHERE scope = 'library' AND scope_ref = ?",
                     (str(library_id),))
        if file_ids:
            marks = ",".join("?" * len(file_ids))
            conn.execute(
                f"DELETE FROM anchors WHERE scope = 'file' AND scope_ref IN ({marks})",
                file_ids)
        conn.execute("DELETE FROM anchors WHERE library_id = ?", (library_id,))
        # Remove now-orphaned photos only (a photo with a copy in another library
        # must survive).
        for pid in photo_ids:
            still = conn.execute(
                "SELECT 1 FROM files WHERE photo_id = ? LIMIT 1", (pid,)).fetchone()
            if still is None:
                conn.execute("DELETE FROM photos WHERE id = ?", (pid,))
        conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(file_ids)
