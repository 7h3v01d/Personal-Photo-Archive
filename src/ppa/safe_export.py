"""Archive-safe user-directed output helpers.

User-selected export paths are never allowed to target a registered source
Library or PPA's operational state.  Writes are atomic via a sibling temporary
file so an existing symlink/hard-link output alias is never opened for writing.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection
from typing import Callable, Iterator


class ArchiveOutputSafetyError(ValueError):
    """Raised when a requested export destination crosses an archive boundary."""


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


def _db_path_from_conn(conn: Connection | None) -> Path | None:
    if conn is None:
        return None
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            # row: seq, name, file
            if row[1] == "main" and row[2]:
                return Path(row[2])
    except Exception:
        return None
    return None


def _borrow_conn(conn: Connection | None, config):
    if conn is not None:
        return conn, False
    db_path = getattr(config, "db_path", None) if config is not None else None
    if db_path is None or not Path(db_path).exists():
        return None, False
    try:
        opened = sqlite3.connect(Path(db_path))
        opened.row_factory = sqlite3.Row
        return opened, True
    except sqlite3.Error:
        return None, False


def _library_roots(conn: Connection | None, config) -> tuple[str, ...]:
    roots: set[str] = set()
    borrowed, must_close = _borrow_conn(conn, config)
    try:
        if borrowed is not None:
            try:
                columns = {r[1] for r in borrowed.execute("PRAGMA table_info(libraries)").fetchall()}
                column = (
                    "root_canonical_path" if "root_canonical_path" in columns
                    else "canonical_path" if "canonical_path" in columns
                    else None
                )
                if column is not None:
                    rows = borrowed.execute(f"SELECT {column} FROM libraries ORDER BY id").fetchall()
                    roots.update(_canonical(Path(r[0])) for r in rows if r[0])
            except sqlite3.Error:
                pass
    finally:
        if must_close and borrowed is not None:
            borrowed.close()
    if config is not None:
        for path in getattr(config, "library_directories", ()) or ():
            roots.add(_canonical(Path(path)))
    return tuple(sorted(roots))


def _operational_paths(conn: Connection | None, config) -> tuple[tuple[str, bool], ...]:
    """Return (canonical path, is_tree) protected operational destinations."""
    protected: list[tuple[str, bool]] = []
    db_path = _db_path_from_conn(conn)
    if db_path is None and config is not None and getattr(config, "db_path", None) is not None:
        db_path = Path(config.db_path)
    if db_path is not None:
        db = Path(db_path)
        protected.extend([
            (_canonical(db), False),
            (_canonical(Path(str(db) + "-wal")), False),
            (_canonical(Path(str(db) + "-shm")), False),
            (_canonical(db.parent / "thumbnails"), True),
            (_canonical(db.parent / "recovery-preservation"), True),
        ])
    if config is not None and getattr(config, "log_path", None) is not None:
        log = Path(config.log_path)
        structured = log.with_name(log.stem + ".jsonl")
        protected.extend([
            (_canonical(log), False),
            (_canonical(structured), False),
        ])

    # Phase 14 permits an internal API caller to choose an alternate operational
    # preservation root.  Once a successful preservation stage records such a
    # root, ordinary exports must protect that tree exactly like the default
    # db-adjacent recovery-preservation directory.
    borrowed, must_close = _borrow_conn(conn, config)
    try:
        if borrowed is not None:
            try:
                table = borrowed.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='archive_recovery_preservation_stages'"
                ).fetchone()
                if table is not None:
                    for row in borrowed.execute(
                        "SELECT DISTINCT preservation_root "
                        "FROM archive_recovery_preservation_stages "
                        "WHERE preservation_root IS NOT NULL"
                    ):
                        if row[0]:
                            protected.append((_canonical(Path(row[0])), True))
            except sqlite3.Error:
                pass
    finally:
        if must_close and borrowed is not None:
            borrowed.close()
    return tuple(protected)


def validate_export_destination(
    destination: str | Path,
    *,
    conn: Connection | None = None,
    config=None,
) -> Path:
    """Validate and return an absolute output path.

    Fail closed when the resolved destination is inside any registered source
    Library, aliases a catalogued source File, or collides with protected PPA
    operational state.  The destination may be outside those trees even if it
    already exists; actual writers use atomic replacement rather than opening
    the existing destination inode for writing.
    """
    raw = Path(destination).expanduser()
    absolute = raw if raw.is_absolute() else Path.cwd() / raw
    canonical = _canonical(absolute)

    for root in _library_roots(conn, config):
        if canonical == root or _within(canonical, root):
            raise ArchiveOutputSafetyError(
                "Export destination is inside a registered source Library; "
                "choose a location outside the archive tree."
            )

    for protected, is_tree in _operational_paths(conn, config):
        if canonical == protected or (is_tree and _within(canonical, protected)):
            raise ArchiveOutputSafetyError(
                "Export destination collides with protected PPA operational state."
            )

    borrowed, must_close = _borrow_conn(conn, config)
    try:
        if borrowed is not None:
            # Canonical-path equality catches leaf/parent symlink aliases.
            try:
                for row in borrowed.execute("SELECT path FROM files WHERE path IS NOT NULL"):
                    if row[0] and _canonical(Path(row[0])) == canonical:
                        raise ArchiveOutputSafetyError(
                            "Export destination resolves to a catalogued source File."
                        )
            except sqlite3.Error:
                pass

            # Existing hard-link aliases outside a Library are rejected too.
            if absolute.exists():
                try:
                    st = absolute.stat()
                    hit = borrowed.execute(
                        "SELECT id FROM files WHERE fs_device_id=? AND fs_object_id=? LIMIT 1",
                        (str(getattr(st, "st_dev", "")), str(getattr(st, "st_ino", ""))),
                    ).fetchone()
                except (OSError, sqlite3.Error):
                    hit = None
                if hit is not None:
                    raise ArchiveOutputSafetyError(
                        "Export destination is the same filesystem object as a catalogued source File."
                    )
    finally:
        if must_close and borrowed is not None:
            borrowed.close()

    return absolute.resolve(strict=False)


@contextmanager
def safe_export_temp(
    destination: str | Path,
    *,
    conn: Connection | None = None,
    config=None,
) -> Iterator[tuple[Path, Path]]:
    """Yield (validated destination, sibling temp path), then atomically commit.

    The existing destination is never opened for writing.  A second validation
    is performed immediately before ``os.replace`` so ordinary path changes
    during export also fail closed.
    """
    out = validate_export_destination(destination, conn=conn, config=config)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=str(out.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        yield out, tmp
        validate_export_destination(out, conn=conn, config=config)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def safe_export_text(
    destination: str | Path,
    contents: str,
    *,
    conn: Connection | None = None,
    config=None,
    encoding: str = "utf-8",
) -> Path:
    with safe_export_temp(destination, conn=conn, config=config) as (out, tmp):
        with tmp.open("w", encoding=encoding, newline="") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
    return out


def safe_export_bytes(
    destination: str | Path,
    contents: bytes,
    *,
    conn: Connection | None = None,
    config=None,
) -> Path:
    with safe_export_temp(destination, conn=conn, config=config) as (out, tmp):
        with tmp.open("wb") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
    return out
