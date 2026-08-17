"""Database connection and initialisation.

Responsible for opening the catalogue database and applying schema.sql on
first run. Deliberately does nothing clever: no ORM, no migration framework
yet (Phase 0 requires schema changes be "migratable", not that a migration
framework exists on day one — sqlite's schema_version table is enough for
now and a real migration runner can be added when there's a second schema
version to migrate to).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection to the catalogue database, creating/initialising it
    if necessary. Safe to call repeatedly.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Safety / integrity pragmas. foreign_keys is OFF by default in sqlite
    # and must be enabled per-connection.
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")

    _apply_schema(conn)
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS version FROM schema_version"
    ).fetchone()
    return row["version"] if row and row["version"] is not None else 0
