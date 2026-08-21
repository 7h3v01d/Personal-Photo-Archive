"""Library (resource) management: listing and safe removal."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_forget_library_removes_owned_anchors(tmp_path):
    from ppa import anchors
    lib = tmp_path / "A"; _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib)
    lid = catalogue.list_libraries(conn)[0].id
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "library", str(lid), "exact", "2004-12-25")
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    assert len(anchors.list_anchors(conn)) == 2

    catalogue.forget_library(conn, lid)
    assert anchors.list_anchors(conn) == []           # owned anchors gone with the library


def test_forgotten_library_anchor_never_attaches_to_reused_id(tmp_path):
    # A -> anchor -> forget A -> B reuses id 1 -> B must NOT inherit A's anchor.
    from ppa import anchors
    a = tmp_path / "A"; _img(a / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, a)
    ida = catalogue.list_libraries(conn)[0].id
    anchors.add_anchor(conn, "library", str(ida), "exact", "2004-12-25", note="Christmas")

    catalogue.forget_library(conn, ida)

    b = tmp_path / "B"; _img(b / "b.jpg")
    scan_library(conn, b)
    idb = catalogue.list_libraries(conn)[0].id
    assert idb == ida                                 # SQLite reused the integer id
    resolved = anchors.resolve_for(anchors.list_anchors(conn),
                                   file_id="x", directory="", library_id=idb)
    assert resolved is None                           # no cross-resource contamination


def test_directory_anchor_does_not_cross_libraries(tmp_path):
    from ppa import anchors
    la, lb = tmp_path / "LibA", tmp_path / "LibB"
    _img(la / "trip" / "p.jpg"); _img(lb / "trip" / "q.jpg", "blue")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, la); scan_library(conn, lb)
    ids = {L.display_path[-4:]: L.id for L in catalogue.list_libraries(conn)}
    anchors.add_anchor(conn, "directory", "trip", "exact", "2004-12-25",
                       library_id=ids["LibA"])
    a = anchors.list_anchors(conn)
    assert anchors.resolve_for(a, file_id="x", directory="trip", library_id=ids["LibB"]) is None
    assert anchors.resolve_for(a, file_id="x", directory="trip", library_id=ids["LibA"]) is not None


def test_unowned_directory_anchor_is_rejected(tmp_path):
    from ppa import anchors
    lib = tmp_path / "A"; _img(lib / "trip" / "p.jpg")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib)
    with pytest.raises(ValueError):
        anchors.add_anchor(conn, "directory", "trip", "exact", "2004-12-25")  # no owner


def test_anchor_for_nonexistent_file_is_rejected(tmp_path):
    from ppa import anchors
    conn = connect(tmp_path / "c.sqlite3")
    with pytest.raises(ValueError):
        anchors.add_anchor(conn, "file", "nonexistent-file-id", "exact", "2004-12-25")


def test_anchor_for_nonexistent_library_is_rejected(tmp_path):
    from ppa import anchors
    conn = connect(tmp_path / "c.sqlite3")
    with pytest.raises(ValueError):
        anchors.add_anchor(conn, "library", "9999", "exact", "2004-12-25")


def test_legacy_null_owner_directory_anchor_is_not_applied(tmp_path):
    # A row from before ownership was enforced (inserted directly, owner NULL)
    # must not resolve for any library — missing provenance is not global.
    from datetime import datetime, timezone
    from ppa import anchors
    a, b = tmp_path / "LibA", tmp_path / "LibB"
    _img(a / "trip" / "p.jpg"); _img(b / "trip" / "q.jpg", "blue")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, a); scan_library(conn, b)
    conn.execute(
        "INSERT INTO anchors (scope, scope_ref, kind, start_date, end_date, note, "
        "created_at, library_id) VALUES ('directory','trip','exact','2004-12-25',"
        "NULL,NULL,?,NULL)", (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    ids = {L.display_path[-4:]: L.id for L in catalogue.list_libraries(conn)}
    a_list = anchors.list_anchors(conn)
    assert anchors.resolve_for(a_list, file_id="x", directory="trip", library_id=ids["LibA"]) is None
    assert anchors.resolve_for(a_list, file_id="x", directory="trip", library_id=ids["LibB"]) is None


def test_migration_009_backfills_only_files_not_library_or_directory(tmp_path):
    # Only durable (UUID) file ownership is backfilled. Library ids are reusable
    # and directory paths are ambiguous, so both stay dormant.
    from datetime import datetime, timezone
    from ppa import anchors
    lib = tmp_path / "L"; _img(lib / "trip" / "p.jpg")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib)
    lid = catalogue.list_libraries(conn)[0].id
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    now = datetime.now(timezone.utc).isoformat()
    for scope, ref in (("library", str(lid)), ("file", fid), ("directory", "trip")):
        conn.execute("INSERT INTO anchors (scope, scope_ref, kind, start_date, end_date, "
                     "note, created_at, library_id) VALUES (?,?,?,?,?,?,?,NULL)",
                     (scope, ref, "exact", "2004-12-25", None, None, now))
    conn.commit()
    conn.executescript(
        "UPDATE anchors SET library_id = (SELECT f.library_id FROM files f "
        "WHERE f.id = anchors.scope_ref) WHERE scope='file' AND library_id IS NULL "
        "AND EXISTS(SELECT 1 FROM files f WHERE f.id = anchors.scope_ref);")
    conn.commit()
    owners = {a.scope: a.library_id for a in anchors.list_anchors(conn)}
    assert owners["file"] == lid                  # durable UUID -> recovered
    assert owners["library"] is None              # reusable id -> NOT guessed
    assert owners["directory"] is None            # ambiguous path -> dormant


def test_migration_009_does_not_attach_legacy_library_anchor_to_reused_id(tmp_path):
    # The exact upgrade-time contamination path: old A(id=1) left a NULL-owner
    # library anchor; B reuses id=1; running 009 must NOT adopt it for B.
    from datetime import datetime, timezone
    from ppa import anchors
    b = tmp_path / "B"; _img(b / "b.jpg")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, b)
    idb = catalogue.list_libraries(conn)[0].id
    conn.execute("INSERT INTO anchors (scope, scope_ref, kind, start_date, end_date, "
                 "note, created_at, library_id) VALUES "
                 "('library', ?, 'exact', '2004-12-25', NULL, 'legacy A', ?, NULL)",
                 (str(idb), datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.executescript(
        "UPDATE anchors SET library_id = (SELECT f.library_id FROM files f "
        "WHERE f.id = anchors.scope_ref) WHERE scope='file' AND library_id IS NULL "
        "AND EXISTS(SELECT 1 FROM files f WHERE f.id = anchors.scope_ref);")
    conn.commit()
    a = anchors.list_anchors(conn)[0]
    assert a.library_id is None                   # stayed dormant, not adopted by B
    assert anchors.resolve_for(anchors.list_anchors(conn),
                               file_id="x", directory="", library_id=idb) is None
