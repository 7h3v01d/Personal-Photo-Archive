"""Safe Library Scanner (Phase 1 + Phase 2).

Recursively inspects a library directory and reconciles what it finds
against the catalogue, without ever writing to a source file.

Phase 2 changes the basis of identity
--------------------------------------
Phase 1 detected moves/renames with a filename+size *heuristic*, because it
had no content hash to confirm identity. Phase 2 hashes file content
(SHA-256) and reconciles on that. Content identity is now a fact, not a
guess, which lets the scanner distinguish the cases the Phase 2 exit
criteria require:

    unchanged   same path, same content
    modified    same path, content changed (caught even if size is identical)
    moved       content that used to live at path X now lives only at path Y
    duplicated  the same content now exists at two places at once
    missing     a catalogued file's path is gone and its content wasn't
                found anywhere else
    restored    a previously-missing file's content reappeared

To tell "moved" apart from "duplicated" you must know whether the original
path still exists, which isn't knowable until the whole tree has been
walked. So the scan is deliberately two-pass:

    Pass 1  inventory the filesystem (stat, verify, hash where needed)
    Pass 2  reconcile that inventory against the catalogue by path + hash

Performance note: Pass 1 does *not* re-hash a file that is already
catalogued at the same path with an unchanged size and mtime — it trusts
the stored hash. That keeps routine re-scans of a 10,000+ photo library
fast. The paranoid, re-hash-everything integrity check is a separate,
explicit operation (see ppa.integrity.verify_library).

Every path change is written to file_path_history and every notable
transition to integrity_events, so nothing is silently overwritten.
Read-only with respect to every source file, always.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection, Row

from PIL import Image, UnidentifiedImageError

from ppa.formats import is_recognised_but_unsupported, supported_extensions
from ppa.hashing import sha256_file
from ppa.logging_setup import get_logger

log = get_logger("scanner")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fmt_mtime(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


@dataclass
class ScanReport:
    session_id: str
    library_path: str
    started_at: str
    completed_at: str | None = None

    new_files: int = 0
    known_files: int = 0
    modified_files: int = 0
    moved_files: int = 0
    duplicate_files: int = 0
    restored_files: int = 0
    missing_files: int = 0
    unsupported_files: int = 0
    deferred_format_files: int = 0
    hashed_files: int = 0  # how many files we actually read+hashed this run
    inaccessible_files: list[tuple[str, str]] = field(default_factory=list)

    # Legacy Phase 1 field. Hashing removes the filename/size guesswork, so
    # Phase 2 never populates this; kept so existing callers don't break.
    renamed_candidates: int = 0

    @property
    def files_scanned(self) -> int:
        return (
            self.new_files
            + self.known_files
            + self.modified_files
            + self.moved_files
            + self.duplicate_files
            + self.restored_files
            + self.renamed_candidates
        )

    def summary(self) -> str:
        lines = [
            f"Scan of {self.library_path}",
            f"  new:                {self.new_files}",
            f"  known/unchanged:    {self.known_files}",
            f"  modified:           {self.modified_files}",
            f"  moved:              {self.moved_files}",
            f"  duplicates:         {self.duplicate_files}",
            f"  restored:           {self.restored_files}",
            f"  missing:            {self.missing_files}",
            f"  unsupported:        {self.unsupported_files}",
            f"  deferred format:    {self.deferred_format_files}",
            f"  hashed this run:    {self.hashed_files}",
            f"  inaccessible:       {len(self.inaccessible_files)}",
        ]
        return "\n".join(lines)


@dataclass
class _DiskFile:
    """One supported, readable file found on disk during Pass 1, with its
    identity resolved (hash freshly computed or trusted from the catalogue).
    """

    path: str
    ext: str
    size_bytes: int
    fs_mtime: str
    width: int
    height: int
    mime_type: str
    sha256: str
    hashed_now: bool
    known_row: Row | None  # active catalogue row at this exact path, if any


def scan_library(
    conn: Connection,
    library_path: Path,
    progress_cb: Callable[[str], None] | None = None,
) -> ScanReport:
    """Scan ``library_path`` recursively and reconcile it against the
    catalogue. Read-only with respect to every file under ``library_path``;
    all writes go to the catalogue database only.

    ``progress_cb`` is an optional callable invoked with short human-readable
    status strings during the scan (for a UI status bar). It has no effect on
    behaviour and defaults to no callback.
    """
    def _progress(msg: str) -> None:
        if progress_cb is not None:
            progress_cb(msg)
    library_path = Path(library_path)
    session_id = str(uuid.uuid4())
    started_at = _now()
    report = ScanReport(
        session_id=session_id,
        library_path=str(library_path),
        started_at=started_at,
    )

    conn.execute(
        "INSERT INTO import_sessions (id, library_path, started_at) VALUES (?, ?, ?)",
        (session_id, str(library_path), started_at),
    )
    conn.commit()

    # Catalogue rows we reconcile against. We care about anything that could
    # still refer to a live-or-returning file: active and missing.
    cat_rows: list[Row] = conn.execute(
        "SELECT * FROM files WHERE status IN ('active', 'missing')"
    ).fetchall()
    active_by_path: dict[str, Row] = {
        r["path"]: r for r in cat_rows if r["status"] == "active"
    }

    # --- Pass 1: inventory the filesystem (read-only) ---------------------
    _progress("Scanning library…")
    disk_files: list[_DiskFile] = []
    seen_count = 0
    for root, _dirs, filenames in os.walk(library_path):
        for name in sorted(filenames):  # sorted -> reproducible ordering
            path = Path(root) / name
            ext = path.suffix.lower()
            extensions = supported_extensions()

            seen_count += 1
            if seen_count % 25 == 0:
                _progress(f"Inventorying… {seen_count} files seen")

            if ext in extensions:
                df = _inventory_supported_file(
                    report=report,
                    path=path,
                    ext=ext,
                    format_map=extensions,
                    active_by_path=active_by_path,
                )
                if df is not None:
                    disk_files.append(df)
            elif is_recognised_but_unsupported(ext):
                report.deferred_format_files += 1
                log.debug("Deferred format, skipping: %s", path)
            else:
                report.unsupported_files += 1
                log.debug("Unsupported extension, skipping: %s", path)

    disk_files.sort(key=lambda d: d.path)  # deterministic reconcile order
    disk_paths = {d.path for d in disk_files}

    # --- Pass 2: reconcile inventory against the catalogue ----------------
    _progress(f"Reconciling {len(disk_files)} files against the catalogue…")
    matched_row_ids: set[str] = set()

    # A live hash index, seeded from the catalogue and extended as we create
    # or relocate rows, so two identical *new* files in one scan resolve as
    # original + duplicate rather than two independents.
    hash_index: dict[str, list[Row]] = {}
    for r in cat_rows:
        if r["sha256"]:
            hash_index.setdefault(r["sha256"], []).append(r)

    # 2a. Files sitting at a path we already know (active). These are the
    #     originals still in place; resolve them before anything else so the
    #     move-vs-duplicate decision below has a correct "still present" set.
    orphans: list[_DiskFile] = []
    for df in disk_files:
        row = df.known_row
        if row is None:
            orphans.append(df)
            continue
        _reconcile_known_path(conn, report, session_id, df, row)
        matched_row_ids.add(row["id"])

    # 2b. Files at a path we don't recognise: move, duplicate, restore, or
    #     genuinely new — decided by content hash.
    for df in orphans:
        _reconcile_orphan(
            conn=conn,
            report=report,
            session_id=session_id,
            df=df,
            hash_index=hash_index,
            matched_row_ids=matched_row_ids,
            disk_paths=disk_paths,
        )

    # 2c. Catalogue files never accounted for and no longer on disk -> missing.
    #     Only raise the event on the active -> missing transition; a file
    #     already recorded missing stays missing quietly.
    for row in cat_rows:
        if row["id"] in matched_row_ids:
            continue
        if row["path"] in disk_paths:
            continue  # its path exists on disk; a duplicate/move claimed it
        if row["status"] == "active":
            conn.execute(
                "UPDATE files SET status = 'missing' WHERE id = ?", (row["id"],)
            )
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
                "VALUES (?, 'missing', ?, ?)",
                (row["id"], f"Not found during scan of {library_path}", session_id),
            )
            report.missing_files += 1

    report.completed_at = _now()
    conn.execute(
        """
        UPDATE import_sessions
        SET completed_at = ?, files_scanned = ?, files_new = ?,
            files_modified = ?, files_missing = ?, files_errored = ?
        WHERE id = ?
        """,
        (
            report.completed_at,
            report.files_scanned,
            report.new_files,
            report.modified_files,
            report.missing_files,
            len(report.inaccessible_files),
            session_id,
        ),
    )
    conn.commit()

    log.info("Scan complete: session=%s\n%s", session_id, report.summary())
    return report


def _inventory_supported_file(
    *,
    report: ScanReport,
    path: Path,
    ext: str,
    format_map: dict[str, str],
    active_by_path: dict[str, Row],
) -> _DiskFile | None:
    """Pass 1 worker: stat, verify the image decodes, and resolve identity.

    Returns a _DiskFile, or None if the file is inaccessible/corrupt (which
    is recorded on the report). Reads bytes only; never writes.
    """
    try:
        stat = path.stat()
    except OSError as exc:
        report.inaccessible_files.append((str(path), str(exc)))
        log.warning("Inaccessible file (stat failed): %s (%s)", path, exc)
        return None

    size_bytes = stat.st_size
    fs_mtime = _fmt_mtime(stat.st_mtime)

    try:
        with Image.open(path) as img:
            img.verify()
        # verify() invalidates the handle; reopen to read dimensions.
        with Image.open(path) as img:
            width, height = img.size
    except (UnidentifiedImageError, OSError) as exc:
        report.inaccessible_files.append((str(path), str(exc)))
        log.warning("Inaccessible/corrupt image, skipping: %s (%s)", path, exc)
        return None

    known = active_by_path.get(str(path))

    # Fast path: catalogued here already, already hashed, and neither size
    # nor mtime moved -> trust the stored hash instead of re-reading bytes.
    if (
        known is not None
        and known["sha256"]
        and known["size_bytes"] == size_bytes
        and known["fs_mtime"] == fs_mtime
    ):
        sha = known["sha256"]
        hashed_now = False
    else:
        try:
            sha = sha256_file(path)
        except OSError as exc:
            report.inaccessible_files.append((str(path), str(exc)))
            log.warning("Could not hash file, skipping: %s (%s)", path, exc)
            return None
        hashed_now = True
        report.hashed_files += 1

    return _DiskFile(
        path=str(path),
        ext=ext,
        size_bytes=size_bytes,
        fs_mtime=fs_mtime,
        width=width,
        height=height,
        mime_type=f"image/{format_map[ext].lower()}",
        sha256=sha,
        hashed_now=hashed_now,
        known_row=known,
    )


def _reconcile_known_path(
    conn: Connection,
    report: ScanReport,
    session_id: str,
    df: _DiskFile,
    row: Row,
) -> None:
    """A file found at a path we already have an active row for."""
    now = _now()

    if row["sha256"] is None:
        # Phase 1 catalogue being upgraded: backfill the hash. Content is
        # whatever is there now; treat as known/unchanged, not modified.
        report.known_files += 1
        conn.execute(
            """
            UPDATE files
            SET sha256 = ?, hash_computed_at = ?, size_bytes = ?, fs_mtime = ?,
                width_px = ?, height_px = ?, mime_type = ?,
                last_seen_at = ?, last_seen_session = ?
            WHERE id = ?
            """,
            (
                df.sha256, now, df.size_bytes, df.fs_mtime, df.width, df.height,
                df.mime_type, now, session_id, row["id"],
            ),
        )
        return

    if row["sha256"] == df.sha256:
        report.known_files += 1
        conn.execute(
            """
            UPDATE files
            SET last_seen_at = ?, last_seen_session = ?, width_px = ?,
                height_px = ?, mime_type = ?, fs_mtime = ?, size_bytes = ?
            WHERE id = ?
            """,
            (now, session_id, df.width, df.height, df.mime_type, df.fs_mtime,
             df.size_bytes, row["id"]),
        )
        return

    # Same path, different content -> a genuine in-place modification. Caught
    # even when the byte size is identical, which Phase 1 could not do.
    report.modified_files += 1
    conn.execute(
        """
        UPDATE files
        SET sha256 = ?, hash_computed_at = ?, size_bytes = ?, fs_mtime = ?,
            width_px = ?, height_px = ?, mime_type = ?,
            last_seen_at = ?, last_seen_session = ?
        WHERE id = ?
        """,
        (
            df.sha256, now, df.size_bytes, df.fs_mtime, df.width, df.height,
            df.mime_type, now, session_id, row["id"],
        ),
    )
    conn.execute(
        "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
        "VALUES (?, 'content_modified', ?, ?)",
        (
            row["id"],
            f"Content changed in place. Old sha256 {row['sha256'][:12]}..., "
            f"new {df.sha256[:12]}...; size {row['size_bytes']} -> {df.size_bytes} bytes.",
            session_id,
        ),
    )


def _reconcile_orphan(
    *,
    conn: Connection,
    report: ScanReport,
    session_id: str,
    df: _DiskFile,
    hash_index: dict[str, list[Row]],
    matched_row_ids: set[str],
    disk_paths: set[str],
) -> None:
    """A file at a path we don't recognise. Its content hash decides whether
    it's a move, an exact duplicate, a restoration, or genuinely new.
    """
    now = _now()
    candidates = [
        r for r in hash_index.get(df.sha256, []) if r["id"] not in matched_row_ids
    ]

    # Prefer a candidate whose own path is gone from disk -> its content
    # relocated here (a move or a restore), rather than a still-present twin.
    relocated = next((r for r in candidates if r["path"] not in disk_paths), None)
    if relocated is not None:
        _apply_relocation(conn, report, session_id, df, relocated, now)
        matched_row_ids.add(relocated["id"])
        _reindex(hash_index, relocated["id"], df.sha256, conn)
        return

    # Otherwise, if any catalogued file with this content still exists, this
    # is an exact duplicate: a new File of the same logical Photo.
    twin = candidates[0] if candidates else None
    if twin is None:
        # Or a twin already matched this scan (e.g. two identical new files):
        already = hash_index.get(df.sha256, [])
        twin = already[0] if already else None

    if twin is not None:
        new_row = _insert_file(
            conn, session_id, df, now, photo_id=twin["photo_id"]
        )
        report.duplicate_files += 1
        conn.execute(
            "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
            "VALUES (?, 'exact_duplicate', ?, ?)",
            (
                new_row["id"],
                f"Byte-identical to existing file {twin['id']} "
                f"(sha256 {df.sha256[:12]}...); linked to the same Photo.",
                session_id,
            ),
        )
        matched_row_ids.add(new_row["id"])
        hash_index.setdefault(df.sha256, []).append(new_row)
        return

    # Genuinely new content -> new Photo + File.
    photo_id = str(uuid.uuid4())
    conn.execute("INSERT INTO photos (id) VALUES (?)", (photo_id,))
    new_row = _insert_file(conn, session_id, df, now, photo_id=photo_id)
    report.new_files += 1
    matched_row_ids.add(new_row["id"])
    hash_index.setdefault(df.sha256, []).append(new_row)


def _apply_relocation(
    conn: Connection,
    report: ScanReport,
    session_id: str,
    df: _DiskFile,
    row: Row,
    now: str,
) -> None:
    """Reassign an existing file row to a new path, confirmed by hash.

    If the row was 'missing', its content reappearing is a restoration;
    otherwise it's a confirmed move. Either way the old path is preserved in
    file_path_history and the transition is logged.
    """
    was_missing = row["status"] == "missing"
    conn.execute(
        "INSERT INTO file_path_history (file_id, path, observed_at, session_id) "
        "VALUES (?, ?, ?, ?)",
        (row["id"], df.path, now, session_id),
    )
    conn.execute(
        """
        UPDATE files
        SET path = ?, filename = ?, extension = ?, status = 'active',
            size_bytes = ?, fs_mtime = ?, width_px = ?, height_px = ?,
            mime_type = ?, sha256 = ?, hash_computed_at = COALESCE(hash_computed_at, ?),
            last_seen_at = ?, last_seen_session = ?
        WHERE id = ?
        """,
        (
            df.path, Path(df.path).name, df.ext, df.size_bytes, df.fs_mtime,
            df.width, df.height, df.mime_type, df.sha256, now, now, session_id,
            row["id"],
        ),
    )
    if was_missing:
        report.restored_files += 1
        event, detail = "restored", (
            f"Previously-missing content reappeared at {df.path} "
            f"(sha256 {df.sha256[:12]}...). Was at {row['path']}."
        )
    else:
        report.moved_files += 1
        event, detail = "move_confirmed", (
            f"Confirmed move by content hash. {row['path']} -> {df.path} "
            f"(sha256 {df.sha256[:12]}...)."
        )
    conn.execute(
        "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
        "VALUES (?, ?, ?, ?)",
        (row["id"], event, detail, session_id),
    )


def _insert_file(
    conn: Connection,
    session_id: str,
    df: _DiskFile,
    now: str,
    *,
    photo_id: str,
) -> Row:
    """Insert a new files row for ``df`` under ``photo_id`` and return it."""
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO files (
            id, photo_id, path, filename, extension, size_bytes, fs_mtime,
            width_px, height_px, mime_type, sha256, hash_computed_at,
            first_seen_at, last_seen_at, first_seen_session, last_seen_session
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id, photo_id, df.path, Path(df.path).name, df.ext,
            df.size_bytes, df.fs_mtime, df.width, df.height, df.mime_type,
            df.sha256, now, now, now, session_id, session_id,
        ),
    )
    conn.execute(
        "INSERT INTO file_path_history (file_id, path, observed_at, session_id) "
        "VALUES (?, ?, ?, ?)",
        (file_id, df.path, now, session_id),
    )
    return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def _reindex(
    hash_index: dict[str, list[Row]], row_id: str, sha: str, conn: Connection
) -> None:
    """Refresh a relocated row inside the live hash index so later duplicate
    matching in the same scan sees its new state.
    """
    fresh = conn.execute("SELECT * FROM files WHERE id = ?", (row_id,)).fetchone()
    bucket = hash_index.setdefault(sha, [])
    for i, r in enumerate(bucket):
        if r["id"] == row_id:
            bucket[i] = fresh
            return
    bucket.append(fresh)
