import uuid
from pathlib import Path

from ppa.db import connect, current_schema_version
from ppa.db.connection import discover_migrations


def test_connect_initialises_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "catalogue.sqlite3"
    conn = connect(db_path)

    assert db_path.exists()
    # The catalogue is migrated to the latest available migration.
    assert current_schema_version(conn) == len(discover_migrations())

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    expected = {
        "photos",
        "files",
        "file_path_history",
        "metadata_observations",
        "import_sessions",
        "cameras",
        "integrity_events",
        "schema_version",
        "libraries",
    }
    assert expected.issubset(tables)


def test_photo_and_file_roundtrip(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")

    photo_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    now = "2026-08-17T00:00:00.000Z"

    conn.execute("INSERT INTO photos (id) VALUES (?)", (photo_id,))
    conn.execute(
        """
        INSERT INTO files (
            id, photo_id, path, filename, size_bytes,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, photo_id, "/photos/IMG_0001.JPG", "IMG_0001.JPG", 1024, now, now),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    assert row["photo_id"] == photo_id
    assert row["status"] == "active"  # default applied


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")

    import pytest
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO files (
                id, photo_id, path, filename, size_bytes,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), "nonexistent-photo-id", "/x.jpg", "x.jpg", 1, "now", "now"),
        )
