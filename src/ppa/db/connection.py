"""Database connection and migration runner.

Opens the catalogue database and brings its schema up to date by applying
any pending migrations from ``migrations/`` in order. Each migration is a
numbered ``NNN_name.sql`` file; the runner records which versions have been
applied in the ``schema_version`` table and never re-applies one.

This replaces the earlier "apply schema.sql every time" approach. It is
forward-only (no down-migrations): an archive's schema should accrete, not
roll back, so historical evidence is never dropped by a migration.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection to the catalogue database, creating/initialising and
    migrating it if necessary. Safe to call repeatedly.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Safety / integrity pragmas. foreign_keys is OFF by default in sqlite
    # and must be enabled per-connection.
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    # The GUI reads on one connection while a background worker writes on
    # another (WAL allows one writer + many readers). A short busy timeout
    # makes a reader wait briefly for the writer rather than raising
    # "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000;")

    _run_migrations(conn)
    return conn


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    conn.commit()


def discover_migrations() -> list[tuple[int, Path]]:
    """Return (version, path) for every migration file, ordered by version."""
    found: list[tuple[int, Path]] = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        m = _MIGRATION_RE.match(path.name)
        if m:
            found.append((int(m.group(1)), path))
    found.sort(key=lambda t: t[0])
    return found


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {r["version"] for r in rows}


def _run_migrations(conn: sqlite3.Connection) -> None:
    _ensure_version_table(conn)
    applied = _applied_versions(conn)

    for version, path in discover_migrations():
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        # Each migration is applied atomically: either the whole file and its
        # version record commit together, or nothing does.
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS version FROM schema_version"
    ).fetchone()
    return row["version"] if row and row["version"] is not None else 0
