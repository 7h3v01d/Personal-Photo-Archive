"""Migration-runner invariants (adversarial).

These encode two findings from review that must never regress:
  * a failing migration leaves NOTHING behind (atomic), and
  * an ambiguous version set (duplicate or non-contiguous) fails closed
    before the database is touched.
"""

from __future__ import annotations

import sqlite3

import pytest

from ppa.db import connection as conn_mod
from ppa.db.connection import (
    MigrationError,
    apply_migration,
    discover_migrations,
    run_migrations,
)


def _mem() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def _tables(c) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_good_migration_applies_and_records_version():
    c = _mem()
    conn_mod._ensure_version_table(c)
    apply_migration(c, 1, "CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (7);")
    assert "t" in _tables(c)
    assert c.execute("SELECT x FROM t").fetchone()["x"] == 7
    assert c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] == 1


def test_failed_migration_rolls_back_completely():
    # A migration that creates a table and then hits invalid SQL must leave
    # NEITHER the table NOR a version record behind.
    c = _mem()
    conn_mod._ensure_version_table(c)
    with pytest.raises(sqlite3.OperationalError):
        apply_migration(c, 1, "CREATE TABLE should_rollback (x INTEGER);\nTHIS IS NOT SQL;")
    assert "should_rollback" not in _tables(c)
    assert c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] is None


def test_duplicate_version_numbers_rejected(tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x INTEGER);")
    (tmp_path / "001_b.sql").write_text("CREATE TABLE b (x INTEGER);")
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


def test_noncontiguous_versions_rejected(tmp_path):
    (tmp_path / "001_initial.sql").write_text("CREATE TABLE a (x INTEGER);")
    (tmp_path / "003_skip.sql").write_text("CREATE TABLE c (x INTEGER);")
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


def test_runner_applies_each_migration_once(tmp_path):
    (tmp_path / "001_initial.sql").write_text("CREATE TABLE a (x INTEGER);")
    (tmp_path / "002_more.sql").write_text("CREATE TABLE b (x INTEGER);")
    c = _mem()
    run_migrations(c, tmp_path)
    assert {"a", "b"} <= _tables(c)
    assert c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] == 2
    # Idempotent: a second run applies nothing new and does not error.
    run_migrations(c, tmp_path)
    assert c.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"] == 2


def test_invalid_set_is_rejected_before_touching_db(tmp_path):
    # If discovery fails, no partial application should occur.
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x INTEGER);")
    (tmp_path / "001_b.sql").write_text("CREATE TABLE b (x INTEGER);")
    c = _mem()
    with pytest.raises(MigrationError):
        run_migrations(c, tmp_path)
    assert "a" not in _tables(c) and "b" not in _tables(c)
