"""Database connection and migration runner.

Opens the catalogue database and brings its schema up to date by applying
pending migrations from ``migrations/`` in order. Each migration is a
numbered ``NNN_name.sql`` file; applied versions are recorded in
``schema_version`` and never re-applied. Forward-only: an archive's schema
should accrete, not roll back, so historical evidence is never dropped.

Two properties this runner guarantees (both were adversarial findings):

  * **Atomic.** Each migration and its version record commit together inside
    one explicit transaction. Python's ``executescript`` issues a COMMIT and
    then runs the script outside any transaction, so a bare
    ``executescript`` + ``commit`` leaves partial DDL behind on failure. We
    therefore wrap the migration in ``BEGIN … COMMIT`` and ``ROLLBACK`` on
    error, relying on SQLite's transactional DDL to undo a half-applied
    migration completely.
  * **Fails closed on ambiguous version sets.** Duplicate version numbers
    (``002_a.sql`` + ``002_b.sql``) or gaps (``001`` then ``003``) raise
    before the database is touched, so two different schemas can never claim
    the same version.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


class MigrationError(RuntimeError):
    """Raised for an ambiguous or invalid migration set (fail closed)."""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection to the catalogue database, creating/initialising and
    migrating it if necessary. Safe to call repeatedly.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    # GUI reads on one connection while a worker writes on another (WAL
    # allows one writer + many readers); a short busy timeout makes a reader
    # wait rather than raising "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000;")

    run_migrations(conn)
    return conn


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Return (version, path) for every migration file, ordered by version.

    Fails closed if two files share a version number or if the versions are
    not a contiguous run starting at 1.
    """
    by_version: dict[int, Path] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if not m:
            continue
        version = int(m.group(1))
        if version in by_version:
            raise MigrationError(
                f"Duplicate migration version {version}: "
                f"{by_version[version].name} and {path.name}"
            )
        by_version[version] = path

    versions = sorted(by_version)
    for expected, actual in enumerate(versions, start=1):
        if expected != actual:
            raise MigrationError(
                f"Non-contiguous migrations: expected {expected}, found {actual}. "
                "Migration versions must be a gapless run starting at 1."
            )
    return [(v, by_version[v]) for v in versions]


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


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    return {r["version"] for r in conn.execute("SELECT version FROM schema_version")}


def apply_migration(conn: sqlite3.Connection, version: int, sql: str) -> None:
    """Apply one migration atomically, recording its version in the same
    transaction. On any error, the whole migration is rolled back and the
    exception re-raised — nothing partial survives.

    Migration files must NOT contain their own transaction control
    (BEGIN/COMMIT/ROLLBACK); this function supplies it.
    """
    script = (
        "BEGIN;\n"
        + sql
        + f"\nINSERT INTO schema_version (version) VALUES ({int(version)});\n"
        + "COMMIT;\n"
    )
    try:
        conn.executescript(script)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass  # transaction already rolled back by SQLite
        raise


def run_migrations(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> None:
    pending = discover_migrations(migrations_dir)  # validates before touching db
    _ensure_version_table(conn)
    applied = _applied_versions(conn)
    for version, path in pending:
        if version in applied:
            continue
        apply_migration(conn, version, path.read_text(encoding="utf-8"))


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    return row["version"] if row and row["version"] is not None else 0
