"""Safe Library Scanner (Phase 1).

Recursively inspects a library directory and reconciles what it finds
against the catalogue, without ever writing to a source file.

What this scanner can and can't tell you yet
----------------------------------------------
Move and rename detection here is a **heuristic**, not a proof: it matches
on filename + size (moved) or size alone (renamed candidate), because
Phase 1 has no content hash to confirm identity with. Two different photos
that happen to share a size will occasionally be misclassified as a
rename. That's expected and acceptable at this phase — Phase 2's SHA-256
reconciliation is what turns these into confirmed identity, and anything
this phase gets wrong gets corrected then, not silently baked in as fact.

Every file this scanner touches gets an entry in `file_path_history`, so
even a wrong provisional match is auditable and reversible later.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection, Row

from PIL import Image, UnidentifiedImageError

from ppa.formats import is_recognised_but_unsupported, supported_extensions
from ppa.logging_setup import get_logger

log = get_logger("scanner")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    renamed_candidates: int = 0
    missing_files: int = 0
    unsupported_files: int = 0
    deferred_format_files: int = 0
    inaccessible_files: list[tuple[str, str]] = field(default_factory=list)

    @property
    def files_scanned(self) -> int:
        return (
            self.new_files
            + self.known_files
            + self.modified_files
            + self.moved_files
            + self.renamed_candidates
        )

    def summary(self) -> str:
        lines = [
            f"Scan of {self.library_path}",
            f"  new:                {self.new_files}",
            f"  known/unchanged:    {self.known_files}",
            f"  modified:           {self.modified_files}",
            f"  moved:              {self.moved_files}",
            f"  renamed (candidate):{self.renamed_candidates}",
            f"  missing:            {self.missing_files}",
            f"  unsupported:        {self.unsupported_files}",
            f"  deferred format:    {self.deferred_format_files}",
            f"  inaccessible:       {len(self.inaccessible_files)}",
        ]
        return "\n".join(lines)


def scan_library(conn: Connection, library_path: Path) -> ScanReport:
    """Scan `library_path` recursively and reconcile it against the
    catalogue. Read-only with respect to every file under `library_path`;
    all writes go to the catalogue database only.
    """
    library_path = Path(library_path)
    session_id = str(uuid.uuid4())
    started_at = _now()
    report = ScanReport(session_id=session_id, library_path=str(library_path), started_at=started_at)

    conn.execute(
        "INSERT INTO import_sessions (id, library_path, started_at) VALUES (?, ?, ?)",
        (session_id, str(library_path), started_at),
    )
    conn.commit()

    extensions = supported_extensions()

    active_rows = conn.execute(
        "SELECT id, path, filename, size_bytes FROM files WHERE status = 'active'"
    ).fetchall()
    by_path: dict[str, Row] = {row["path"]: row for row in active_rows}

    unmatched_by_filename_size: dict[tuple[str, int], list[Row]] = {}
    unmatched_by_size: dict[int, list[Row]] = {}
    for row in active_rows:
        unmatched_by_filename_size.setdefault((row["filename"], row["size_bytes"]), []).append(row)
        unmatched_by_size.setdefault(row["size_bytes"], []).append(row)

    seen_file_ids: set[str] = set()

    for root, _dirs, filenames in os.walk(library_path):
        for name in filenames:
            path = Path(root) / name
            ext = path.suffix.lower()

            if ext in extensions:
                _handle_supported_file(
                    conn=conn,
                    report=report,
                    session_id=session_id,
                    path=path,
                    ext=ext,
                    format_map=extensions,
                    by_path=by_path,
                    unmatched_by_filename_size=unmatched_by_filename_size,
                    unmatched_by_size=unmatched_by_size,
                    seen_file_ids=seen_file_ids,
                )
            elif is_recognised_but_unsupported(ext):
                report.deferred_format_files += 1
                log.debug("Deferred format, skipping: %s", path)
            else:
                report.unsupported_files += 1
                log.debug("Unsupported extension, skipping: %s", path)

    for row in active_rows:
        if row["id"] not in seen_file_ids:
            conn.execute("UPDATE files SET status = 'missing' WHERE id = ?", (row["id"],))
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


def _handle_supported_file(
    *,
    conn: Connection,
    report: ScanReport,
    session_id: str,
    path: Path,
    ext: str,
    format_map: dict[str, str],
    by_path: dict[str, Row],
    unmatched_by_filename_size: dict[tuple[str, int], list[Row]],
    unmatched_by_size: dict[int, list[Row]],
    seen_file_ids: set[str],
) -> None:
    try:
        stat = path.stat()
    except OSError as exc:
        report.inaccessible_files.append((str(path), str(exc)))
        log.warning("Inaccessible file (stat failed): %s (%s)", path, exc)
        return

    size_bytes = stat.st_size
    fs_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )

    try:
        with Image.open(path) as img:
            img.verify()
        # verify() invalidates the image handle; reopen to read dimensions.
        with Image.open(path) as img:
            width, height = img.size
    except (UnidentifiedImageError, OSError) as exc:
        report.inaccessible_files.append((str(path), str(exc)))
        log.warning("Inaccessible/corrupt image, skipping: %s (%s)", path, exc)
        return

    mime_type = f"image/{format_map[ext].lower()}"
    now = _now()

    existing = by_path.get(str(path))
    if existing is not None:
        seen_file_ids.add(existing["id"])
        if existing["size_bytes"] == size_bytes:
            report.known_files += 1
            conn.execute(
                """
                UPDATE files
                SET last_seen_at = ?, last_seen_session = ?, width_px = ?,
                    height_px = ?, mime_type = ?, fs_mtime = ?
                WHERE id = ?
                """,
                (now, session_id, width, height, mime_type, fs_mtime, existing["id"]),
            )
        else:
            report.modified_files += 1
            conn.execute(
                """
                UPDATE files
                SET size_bytes = ?, fs_mtime = ?, width_px = ?, height_px = ?,
                    mime_type = ?, last_seen_at = ?, last_seen_session = ?
                WHERE id = ?
                """,
                (
                    size_bytes, fs_mtime, width, height, mime_type, now,
                    session_id, existing["id"],
                ),
            )
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
                "VALUES (?, 'size_changed', ?, ?)",
                (
                    existing["id"],
                    f"Size changed from {existing['size_bytes']} to {size_bytes} bytes; "
                    "content-level confirmation needs Phase 2 hashing.",
                    session_id,
                ),
            )
        return

    candidate = _find_move_or_rename_candidate(
        path, size_bytes, unmatched_by_filename_size, unmatched_by_size, seen_file_ids
    )

    if candidate is not None:
        row, is_same_filename = candidate
        seen_file_ids.add(row["id"])
        event_type = "moved" if is_same_filename else "possibly_renamed"
        if is_same_filename:
            report.moved_files += 1
        else:
            report.renamed_candidates += 1

        conn.execute(
            "INSERT INTO file_path_history (file_id, path, observed_at, session_id) "
            "VALUES (?, ?, ?, ?)",
            (row["id"], str(path), now, session_id),
        )
        conn.execute(
            """
            UPDATE files
            SET path = ?, filename = ?, last_seen_at = ?, last_seen_session = ?,
                width_px = ?, height_px = ?, mime_type = ?, fs_mtime = ?
            WHERE id = ?
            """,
            (str(path), path.name, now, session_id, width, height, mime_type, fs_mtime, row["id"]),
        )
        conn.execute(
            "INSERT INTO integrity_events (file_id, event_type, detail, session_id) "
            "VALUES (?, ?, ?, ?)",
            (
                row["id"],
                event_type,
                f"Provisional match by filename/size, unconfirmed by hash. Old path: {row['path']}",
                session_id,
            ),
        )
        return

    photo_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    conn.execute("INSERT INTO photos (id) VALUES (?)", (photo_id,))
    conn.execute(
        """
        INSERT INTO files (
            id, photo_id, path, filename, extension, size_bytes, fs_mtime,
            width_px, height_px, mime_type, first_seen_at, last_seen_at,
            first_seen_session, last_seen_session
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id, photo_id, str(path), path.name, ext, size_bytes, fs_mtime,
            width, height, mime_type, now, now, session_id, session_id,
        ),
    )
    conn.execute(
        "INSERT INTO file_path_history (file_id, path, observed_at, session_id) "
        "VALUES (?, ?, ?, ?)",
        (file_id, str(path), now, session_id),
    )
    seen_file_ids.add(file_id)
    report.new_files += 1


def _find_move_or_rename_candidate(
    path: Path,
    size_bytes: int,
    unmatched_by_filename_size: dict[tuple[str, int], list[Row]],
    unmatched_by_size: dict[int, list[Row]],
    seen_file_ids: set[str],
) -> tuple[Row, bool] | None:
    """Best-effort move/rename match. Returns (row, is_same_filename) or
    None. See module docstring: this is provisional until Phase 2 hashing
    confirms it.
    """
    key = (path.name, size_bytes)
    for row in unmatched_by_filename_size.get(key, []):
        if row["id"] not in seen_file_ids:
            return row, True

    for row in unmatched_by_size.get(size_bytes, []):
        if row["id"] not in seen_file_ids:
            return row, False

    return None
