"""Library (resource) management: listing and safe removal."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ppa import catalogue, metadata
from ppa.db import connect
from ppa.scanner import scan_library


def _img(p: Path, color="red"):
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(p)


def test_list_libraries_reports_counts_and_availability(tmp_path):
    lib = tmp_path / "A"
    _img(lib / "a.jpg"); _img(lib / "b.jpg", "blue")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib)
    libs = catalogue.list_libraries(conn)
    assert len(libs) == 1
    assert libs[0].present == 2 and libs[0].missing == 0 and libs[0].available is True
    assert libs[0].state == "active"


def test_forget_library_removes_records_but_not_source_files(tmp_path):
    lib = tmp_path / "A"
    _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    lid = catalogue.list_libraries(conn)[0].id

    n = catalogue.forget_library(conn, lid)

    assert n == 1
    assert (lib / "a.jpg").exists()                       # source photo untouched
    assert catalogue.list_libraries(conn) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM file_revisions").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"] == 0
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_forget_library_keeps_photo_with_copy_in_another_library(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    _img(a / "dup.jpg", "blue")
    (b).mkdir(parents=True, exist_ok=True)
    (b / "dup.jpg").write_bytes((a / "dup.jpg").read_bytes())   # identical bytes
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, a); scan_library(conn, b)

    ida = [L.id for L in catalogue.list_libraries(conn) if L.display_path.endswith("A")][0]
    catalogue.forget_library(conn, ida)

    # The shared Photo survives because B still holds a copy.
    assert conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE filename='dup.jpg'").fetchone()["n"] == 1


def test_forget_library_is_atomic_on_bad_id(tmp_path):
    lib = tmp_path / "A"; _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib)
    before = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    # A non-existent library removes nothing and leaves the catalogue intact.
    assert catalogue.forget_library(conn, 9999) == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == before
