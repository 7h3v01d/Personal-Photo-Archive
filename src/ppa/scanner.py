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

    library_id, canonical_root = _resolve_library(conn, library_path)
    walk_base = str(library_path)
    root_prefix = walk_base.rstrip(os.sep) + os.sep + "%"

    conn.execute(
        "INSERT INTO import_sessions (id, library_path, started_at) VALUES (?, ?, ?)",
        (session_id, str(library_path), started_at),
    )
    conn.commit()

    # Reconcile ONLY against files in THIS library — plus any legacy row (no
    # library assigned yet) that physically sits under this library's root,
    # which we adopt on sight. Files belonging to *other* libraries are
    # invisible here, so scanning one library can never mark another's files
    # moved or missing, and identical content in two libraries stays two files.
    cat_rows: list[Row] = conn.execute(
        "SELECT * FROM files WHERE status IN ('active', 'missing') "
        "AND (library_id = ? OR (library_id IS NULL AND path LIKE ?))",
        (library_id, root_prefix),
    ).fetchall()
    active_by_path: dict[str, Row] = {
        r["path"]: r for r in cat_rows if r["status"] == "active"
    }

    # --- Pass 1: inventory the filesystem (read-only) ---------------------
    _progress("Scanning library…")
    disk_files: list[_DiskFile] = []
    seen_paths: set[str] = set()  # every supported file physically present,
    #                               whether or not it could be decoded
    traversal_errors: list[str] = []

    def _on_walk_error(exc: OSError) -> None:
        # A directory we could not read means our view of the filesystem is
        # incomplete. Record it; below we refuse to mark anything missing.
        traversal_errors.append(str(exc))
        log.warning("Traversal error (scan is incomplete): %s", exc)

    seen_count = 0
    for root, _dirs, filenames in os.walk(library_path, onerror=_on_walk_error):
        for name in sorted(filenames):  # sorted -> reproducible ordering
            path = Path(root) / name
            ext = path.suffix.lower()
            extensions = supported_extensions()

            seen_count += 1
            if seen_count % 25 == 0:
                _progress(f"Inventorying… {seen_count} files seen")

            if ext in extensions:
                seen_paths.add(str(path))  # physically present regardless of decode
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
    scan_complete = not traversal_errors

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
        _reconcile_known_path(
            conn, report, session_id, df, row, hash_index, library_id, walk_base
        )
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
            library_id=library_id,
            walk_base=walk_base,
        )

    # 2c. Account for catalogue files not matched above.
    #     - present on disk but not successfully inventoried  -> present + unhealthy
    #       (e.g. corrupt/unreadable), NEVER "missing"
    #     - genuinely absent                                  -> missing, but only
    #       if this scan saw the whole library. An INCOMPLETE traversal must not
    #       be allowed to conclude anything vanished (fail closed).
    for row in cat_rows:
        if row["id"] in matched_row_ids:
            continue
        if row["path"] in disk_paths:
            continue  # its path exists on disk; a duplicate/move claimed it

        if row["path"] in seen_paths:
            # Physically present but we couldn't read/decode it this scan.
            if row["health_status"] != "unreadable":
                conn.execute(
                    "UPDATE files SET presence_status = 'present', "
                    "health_status = 'unreadable' WHERE id = ?",
                    (row["id"],),
                )
                conn.execute(
                    "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
                    "VALUES (?, 'unreadable', ?, ?)",
                    (row["id"],
                     f"Present at {row['path']} but could not be read/decoded during scan.",
                     session_id),
                )
            continue

        if not scan_complete:
            continue  # incomplete view of the filesystem -> refuse to mark missing

        if row["status"] == "active":
            conn.execute(
                "UPDATE files SET status = 'missing', presence_status = 'missing' "
                "WHERE id = ?",
                (row["id"],),
            )
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
                "VALUES (?, 'missing', ?, ?)",
                (row["id"], f"Not found during complete scan of {library_path}", session_id),
            )
            report.missing_files += 1

    report.completed_at = _now()
    conn.execute(
        "UPDATE libraries SET last_scan_at = ? WHERE id = ?",
        (report.completed_at, library_id),
    )
    conn.execute(
        """
        UPDATE import_sessions
        SET completed_at = ?, files_scanned = ?, files_new = ?,
            files_modified = ?, files_missing = ?, files_errored = ?,
            scan_status = ?, traversal_errors = ?
        WHERE id = ?
        """,
        (
            report.completed_at,
            report.files_scanned,
            report.new_files,
            report.modified_files,
            report.missing_files,
            len(report.inaccessible_files),
            "complete" if scan_complete else "incomplete",
            len(traversal_errors),
            session_id,
        ),
    )
    conn.commit()

    log.info("Scan complete: session=%s\n%s", session_id, report.summary())
    return report


def _resolve_library(conn: Connection, root_path: Path) -> tuple[int, str]:
    """Return (library_id, canonical_root) for ``root_path``, creating the
    library row on first sight. Canonicalisation here is a normpath+abspath;
    full Windows case-folding is a later hardening step.
    """
    canonical = os.path.abspath(os.path.normpath(str(root_path)))
    row = conn.execute(
        "SELECT id FROM libraries WHERE root_canonical_path = ?", (canonical,)
    ).fetchone()
    if row is not None:
        return row["id"], canonical
    cur = conn.execute(
        "INSERT INTO libraries (root_display_path, root_canonical_path) VALUES (?, ?)",
        (str(root_path), canonical),
    )
    return int(cur.lastrowid), canonical


def _relative_to(path: str, walk_base: str) -> str:
    try:
        return os.path.relpath(path, walk_base)
    except ValueError:  # e.g. different drive on Windows
        return path


def _index_add(hash_index: dict[str, list[Row]], sha: str | None, row: Row) -> None:
    if sha:
        hash_index.setdefault(sha, []).append(row)


def _index_remove(hash_index: dict[str, list[Row]], sha: str | None, row_id: str) -> None:
    if not sha:
        return
    bucket = hash_index.get(sha)
    if bucket:
        hash_index[sha] = [r for r in bucket if r["id"] != row_id]


def _create_revision(
    conn: Connection, file_id: str, df: _DiskFile, now: str, session_id: str
) -> str:
    """Insert a fresh immutable revision for this file's current content and
    return its id. Revisions are append-only — callers never mutate one.
    """
    rev_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO file_revisions (
            id, file_id, sha256, size_bytes, width_px, height_px, fs_mtime,
            first_observed_at, observed_session, extraction_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (rev_id, file_id, df.sha256, df.size_bytes, df.width, df.height,
         df.fs_mtime, now, session_id),
    )
    return rev_id


def _record_fs_observation(
    conn: Connection, file_id: str, revision_id: str | None, fs_mtime: str | None,
    session_id: str,
) -> None:
    """Keep the filesystem-date observation in step with the file's current
    mtime. The scanner owns this because it is the component that directly
    observes the filesystem; keeping it here means the evidence can never go
    stale relative to files.fs_mtime.
    """
    if not fs_mtime:
        return
    conn.execute(
        "DELETE FROM metadata_observations WHERE file_id = ? AND source = 'filesystem' "
        "AND key = 'mtime'",
        (file_id,),
    )
    conn.execute(
        "INSERT INTO metadata_observations (file_id, file_revision_id, source, key, value, session_id) "
        "VALUES (?, ?, 'filesystem', 'mtime', ?, ?)",
        (file_id, revision_id, fs_mtime, session_id),
    )


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
    hash_index: dict[str, list[Row]],
    library_id: int,
    walk_base: str,
) -> None:
    """A file found at a path we already have an active row for.

    Also adopts a legacy row (one with no library assigned) into this library,
    and — critically — keeps the in-memory hash index in step when content
    changes, so a later file bearing the *old* bytes is not mistaken for a
    duplicate of this now-changed row.
    """
    now = _now()
    rel = _relative_to(df.path, walk_base)

    if row["sha256"] is None:
        # Phase 1 catalogue being upgraded: backfill the hash by completing the
        # (content-less) current revision in place — not a content change.
        report.known_files += 1
        conn.execute(
            """
            UPDATE files
            SET sha256 = ?, hash_computed_at = ?, size_bytes = ?, fs_mtime = ?,
                width_px = ?, height_px = ?, mime_type = ?,
                library_id = ?, relative_path = ?,
                status = 'active', presence_status = 'present', health_status = 'ok',
                last_seen_at = ?, last_seen_session = ?
            WHERE id = ?
            """,
            (
                df.sha256, now, df.size_bytes, df.fs_mtime, df.width, df.height,
                df.mime_type, library_id, rel, now, session_id, row["id"],
            ),
        )
        if row["current_revision_id"]:
            conn.execute(
                "UPDATE file_revisions SET sha256 = ?, size_bytes = ?, width_px = ?, "
                "height_px = ?, fs_mtime = ? WHERE id = ?",
                (df.sha256, df.size_bytes, df.width, df.height, df.fs_mtime,
                 row["current_revision_id"]),
            )
        _record_fs_observation(conn, row["id"], row["current_revision_id"], df.fs_mtime, session_id)
        _index_add(hash_index, df.sha256, _refetch(conn, row["id"]))
        return

    if row["sha256"] == df.sha256:
        report.known_files += 1
        conn.execute(
            """
            UPDATE files
            SET last_seen_at = ?, last_seen_session = ?, width_px = ?,
                height_px = ?, mime_type = ?, fs_mtime = ?, size_bytes = ?,
                library_id = ?, relative_path = ?,
                status = 'active', presence_status = 'present', health_status = 'ok'
            WHERE id = ?
            """,
            (now, session_id, df.width, df.height, df.mime_type, df.fs_mtime,
             df.size_bytes, library_id, rel, row["id"]),
        )
        # Content is unchanged but the filesystem mtime may have been touched;
        # keep the filesystem-date evidence current.
        _record_fs_observation(conn, row["id"], row["current_revision_id"], df.fs_mtime, session_id)
        return

    # Same path, different content -> a genuine in-place modification. Append a
    # new immutable revision and move the pointer; never mutate/delete the old.
    old_sha = row["sha256"]
    report.modified_files += 1
    if row["current_revision_id"]:
        conn.execute(
            "UPDATE file_revisions SET superseded_at = ? WHERE id = ?",
            (now, row["current_revision_id"]),
        )
    new_rev = _create_revision(conn, row["id"], df, now, session_id)
    conn.execute(
        """
        UPDATE files
        SET sha256 = ?, hash_computed_at = ?, size_bytes = ?, fs_mtime = ?,
            width_px = ?, height_px = ?, mime_type = ?,
            library_id = ?, relative_path = ?, current_revision_id = ?,
            status = 'active', presence_status = 'present', health_status = 'ok',
            last_seen_at = ?, last_seen_session = ?
        WHERE id = ?
        """,
        (
            df.sha256, now, df.size_bytes, df.fs_mtime, df.width, df.height,
            df.mime_type, library_id, rel, new_rev, now, session_id, row["id"],
        ),
    )
    # Record the FULL previous hash so the archive never forgets those bytes.
    conn.execute(
        "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
        "VALUES (?, 'content_modified', ?, ?)",
        (
            row["id"],
            f"Content changed in place. previous_sha256={old_sha} "
            f"new_sha256={df.sha256} size {row['size_bytes']} -> {df.size_bytes} bytes.",
            session_id,
        ),
    )
    _record_fs_observation(conn, row["id"], new_rev, df.fs_mtime, session_id)
    # Keep the hash index truthful: this row no longer holds the old content.
    _index_remove(hash_index, old_sha, row["id"])
    _index_add(hash_index, df.sha256, _refetch(conn, row["id"]))


def _refetch(conn: Connection, file_id: str) -> Row:
    return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def _reconcile_orphan(
    *,
    conn: Connection,
    report: ScanReport,
    session_id: str,
    df: _DiskFile,
    hash_index: dict[str, list[Row]],
    matched_row_ids: set[str],
    disk_paths: set[str],
    library_id: int,
    walk_base: str,
) -> None:
    """A file at a path we don't recognise. Its content hash decides whether
    it's a move, an exact duplicate, a restoration, or genuinely new.

    Move-vs-duplicate is decided by whether a same-content row's *own* path is
    still present on disk. This uses the (maintained) hash index and the disk
    inventory directly rather than a matched-set heuristic, so a row whose
    content changed earlier in this scan can never be mistaken for a twin.
    """
    now = _now()
    candidates = hash_index.get(df.sha256, [])

    # A relocation: same content whose own path is GONE from disk (moved here
    # or reappeared after going missing). Never something already claimed.
    relocated = next(
        (r for r in candidates
         if r["id"] not in matched_row_ids and r["path"] not in disk_paths),
        None,
    )
    if relocated is not None:
        _apply_relocation(conn, report, session_id, df, relocated, now,
                          library_id, walk_base)
        matched_row_ids.add(relocated["id"])
        _index_remove(hash_index, df.sha256, relocated["id"])
        _index_add(hash_index, df.sha256, _refetch(conn, relocated["id"]))
        return

    # An exact duplicate: same content whose path is STILL on disk (the
    # original is present), so this is a second File of the same Photo.
    present_twin = next((r for r in candidates if r["path"] in disk_paths), None)
    if present_twin is not None:
        new_row = _insert_file(
            conn, session_id, df, now, photo_id=present_twin["photo_id"],
            library_id=library_id, walk_base=walk_base,
        )
        report.duplicate_files += 1
        conn.execute(
            "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
            "VALUES (?, 'exact_duplicate', ?, ?)",
            (
                new_row["id"],
                f"Byte-identical to existing file {present_twin['id']} "
                f"(sha256 {df.sha256[:12]}...); linked to the same Photo.",
                session_id,
            ),
        )
        matched_row_ids.add(new_row["id"])
        _index_add(hash_index, df.sha256, new_row)
        return

    # Genuinely new content -> new Photo + File.
    photo_id = str(uuid.uuid4())
    conn.execute("INSERT INTO photos (id) VALUES (?)", (photo_id,))
    new_row = _insert_file(conn, session_id, df, now, photo_id=photo_id,
                           library_id=library_id, walk_base=walk_base)
    report.new_files += 1
    matched_row_ids.add(new_row["id"])
    _index_add(hash_index, df.sha256, new_row)


def _apply_relocation(
    conn: Connection,
    report: ScanReport,
    session_id: str,
    df: _DiskFile,
    row: Row,
    now: str,
    library_id: int,
    walk_base: str,
) -> None:
    """Reassign an existing file row to a new path, confirmed by hash.

    If the row was 'missing', its content reappearing is a restoration;
    otherwise it's a confirmed move. Either way the old path is preserved in
    file_path_history and the transition is logged.
    """
    was_missing = row["status"] == "missing"
    rel = _relative_to(df.path, walk_base)
    conn.execute(
        "INSERT INTO file_path_history (file_id, path, observed_at, session_id) "
        "VALUES (?, ?, ?, ?)",
        (row["id"], df.path, now, session_id),
    )
    conn.execute(
        """
        UPDATE files
        SET path = ?, filename = ?, extension = ?, status = 'active',
            presence_status = 'present', health_status = 'ok',
            size_bytes = ?, fs_mtime = ?, width_px = ?, height_px = ?,
            mime_type = ?, sha256 = ?, hash_computed_at = COALESCE(hash_computed_at, ?),
            library_id = ?, relative_path = ?,
            last_seen_at = ?, last_seen_session = ?
        WHERE id = ?
        """,
        (
            df.path, Path(df.path).name, df.ext, df.size_bytes, df.fs_mtime,
            df.width, df.height, df.mime_type, df.sha256, now,
            library_id, rel, now, session_id, row["id"],
        ),
    )
    _record_fs_observation(conn, row["id"], row["current_revision_id"], df.fs_mtime, session_id)
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
    library_id: int,
    walk_base: str,
) -> Row:
    """Insert a new files row for ``df`` under ``photo_id`` and return it.

    Creates the file's first immutable revision and points the file at it.
    """
    file_id = str(uuid.uuid4())
    rel = _relative_to(df.path, walk_base)
    conn.execute(
        """
        INSERT INTO files (
            id, photo_id, library_id, path, relative_path, filename, extension,
            size_bytes, fs_mtime, width_px, height_px, mime_type, sha256,
            hash_computed_at, first_seen_at, last_seen_at, first_seen_session,
            last_seen_session, status, presence_status, health_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  'active', 'present', 'ok')
        """,
        (
            file_id, photo_id, library_id, df.path, rel, Path(df.path).name,
            df.ext, df.size_bytes, df.fs_mtime, df.width, df.height,
            df.mime_type, df.sha256, now, now, now, session_id, session_id,
        ),
    )
    rev_id = _create_revision(conn, file_id, df, now, session_id)
    conn.execute("UPDATE files SET current_revision_id = ? WHERE id = ?", (rev_id, file_id))
    conn.execute(
        "INSERT INTO file_path_history (file_id, path, observed_at, session_id) "
        "VALUES (?, ?, ?, ?)",
        (file_id, df.path, now, session_id),
    )
    _record_fs_observation(conn, file_id, rev_id, df.fs_mtime, session_id)
    return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


