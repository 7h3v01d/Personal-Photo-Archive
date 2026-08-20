"""Hardening 3.2 — archive transaction & identity closure regressions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from ppa import metadata
from ppa.db import connect
import ppa.scanner as sc
from ppa.scanner import scan_library, OverlappingLibraryError


def _img(p: Path, color="red") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(p)


def _exif_jpg(p: Path, dto: str, color="red") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (40, 30), color)
    exif = img.getexif(); sub = exif.get_ifd(ExifTags.IFD.Exif); sub[0x9003] = dto
    img.save(p, format="JPEG", exif=exif)


def _crash_on_second_reconcile(monkeypatch):
    real = sc._reconcile_known_path
    calls = {"n": 0}

    def crashing(*a, **k):
        calls["n"] += 1
        r = real(*a, **k)
        if calls["n"] >= 2:
            raise RuntimeError("crash mid-scan")
        return r

    monkeypatch.setattr(sc, "_reconcile_known_path", crashing)


# --- failed-scan atomicity (the blocker) ------------------------------------


def test_failed_scan_rolls_back_partial_reconciliation(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; _img(lib / "a.jpg"); _img(lib / "b.jpg", "blue")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    good_session = conn.execute(
        "SELECT last_seen_session FROM files WHERE filename='a.jpg'"
    ).fetchone()["last_seen_session"]

    _crash_on_second_reconcile(monkeypatch)
    with pytest.raises(RuntimeError):
        scan_library(conn, lib)

    # No file was left stamped by the failed session.
    sessions = {
        r["last_seen_session"]
        for r in conn.execute("SELECT last_seen_session FROM files")
    }
    assert sessions == {good_session}
    # Session audit is honest.
    assert conn.execute(
        "SELECT scan_status FROM import_sessions ORDER BY started_at DESC LIMIT 1"
    ).fetchone()["scan_status"] == "failed"


def test_failed_scan_does_not_commit_new_revision(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; _img(lib / "a.jpg", "red"); _img(lib / "b.jpg", "blue")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)

    a0 = conn.execute(
        "SELECT sha256, current_revision_id, last_seen_session FROM files WHERE filename='a.jpg'"
    ).fetchone()
    rev_before = conn.execute("SELECT COUNT(*) AS n FROM file_revisions").fetchone()["n"]
    ev_before = conn.execute(
        "SELECT COUNT(*) AS n FROM integrity_events WHERE event_type='content_modified'"
    ).fetchone()["n"]

    _img(lib / "a.jpg", "green")  # a genuine content change...
    _crash_on_second_reconcile(monkeypatch)  # ...but the scan crashes
    with pytest.raises(RuntimeError):
        scan_library(conn, lib)

    a1 = conn.execute(
        "SELECT sha256, current_revision_id, last_seen_session FROM files WHERE filename='a.jpg'"
    ).fetchone()
    assert a1["sha256"] == a0["sha256"]
    assert a1["current_revision_id"] == a0["current_revision_id"]
    assert a1["last_seen_session"] == a0["last_seen_session"]
    assert conn.execute("SELECT COUNT(*) AS n FROM file_revisions").fetchone()["n"] == rev_before
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM integrity_events WHERE event_type='content_modified'"
    ).fetchone()["n"] == ev_before


# --- within-library identity (spelling independence) ------------------------


def test_respelled_library_root_is_not_a_move(tmp_path, monkeypatch):
    # The reviewer's scenario: the SAME physical library opened once by a
    # relative path and once by its absolute path. Different root spellings that
    # resolve to one canonical location must not look like a move. (No symlink
    # needed, so this runs on Windows too.)
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")

    monkeypatch.chdir(tmp_path)
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, Path("lib"))       # relative spelling
    rep = scan_library(conn, tmp_path / "lib")  # absolute spelling, same dir

    assert rep.moved_files == 0
    assert rep.new_files == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 1


def test_respelled_library_root_symlink_variant(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    link = tmp_path / "link"
    try:
        os.symlink(lib, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib)
    rep = scan_library(conn, link)
    assert rep.moved_files == 0 and rep.new_files == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 1


# --- overlapping libraries ---------------------------------------------------


def test_overlapping_library_root_rejected(tmp_path):
    root = tmp_path / "Photos"; _img(root / "2004" / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, root)
    with pytest.raises(OverlappingLibraryError):
        scan_library(conn, root / "2004")     # nested inside existing
    # And the reverse direction.
    conn2 = connect(tmp_path / "c2.sqlite3")
    scan_library(conn2, root / "2004")
    with pytest.raises(OverlappingLibraryError):
        scan_library(conn2, root)             # contains existing


# --- extractor provenance / staleness ---------------------------------------


def test_extractor_version_bump_makes_extraction_stale(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; _exif_jpg(lib / "a.jpg", "2004:12:25 09:14:32")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)

    assert metadata.extract_stale(conn) == 1
    rev = conn.execute("SELECT extractor_version, extraction_status FROM file_revisions").fetchone()
    assert rev["extraction_status"] == "success"
    assert rev["extractor_version"] == metadata.EXTRACTOR_VERSION

    # Nothing stale on a re-run at the same version.
    assert metadata.extract_stale(conn) == 0

    # Bump the extractor version -> the current revision becomes stale again.
    monkeypatch.setattr(metadata, "EXTRACTOR_VERSION", "pillow-exif/2")
    assert metadata.extract_stale(conn) == 1
    rev2 = conn.execute("SELECT extractor_version FROM file_revisions").fetchone()
    assert rev2["extractor_version"] == "pillow-exif/2"


# --- Hardening 3.2.2: canonical paths, offline-aware verify, DB identity -----


def test_files_path_is_always_absolute(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    monkeypatch.chdir(tmp_path)
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, Path("lib"))          # scanned via a RELATIVE spelling
    stored = conn.execute("SELECT path FROM files").fetchone()["path"]
    assert os.path.isabs(stored)             # ...but stored absolute


def test_relative_first_scan_survives_cwd_change(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    monkeypatch.chdir(tmp_path)
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, Path("lib"))          # relative root, cwd = tmp_path

    other = tmp_path / "elsewhere"; other.mkdir()
    monkeypatch.chdir(other)                  # launch PPA from a different cwd
    from ppa.integrity import verify_library
    verify_library(conn)
    presence = conn.execute("SELECT presence_status FROM files").fetchone()["presence_status"]
    assert presence == "present"             # not a false "missing"


def test_verify_skips_unavailable_library(tmp_path):
    from ppa.integrity import verify_library
    A = tmp_path / "A"; B = tmp_path / "B"
    _img(A / "a.jpg"); _img(B / "b.jpg", "blue")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, A); scan_library(conn, B)

    os.rename(A, tmp_path / "A_offline")      # external drive unplugged
    rep = verify_library(conn)

    presence = {r["filename"]: r["presence_status"]
                for r in conn.execute("SELECT filename, presence_status FROM files")}
    assert presence["a.jpg"] == "present"     # unreachable != missing
    assert presence["b.jpg"] == "present"
    assert rep.unavailable_libraries == 1 and rep.skipped_unavailable == 1
    states = [r["state"] for r in conn.execute("SELECT state FROM libraries ORDER BY id")]
    assert "unavailable" in states


def test_duplicate_identity_rejected_by_db(tmp_path):
    import sqlite3, uuid
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    row = conn.execute(
        "SELECT library_id, relative_path_key, photo_id FROM files"
    ).fetchone()
    conn.execute("INSERT INTO photos (id) VALUES (?)", (str(uuid.uuid4()),))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO files (id, photo_id, library_id, path, relative_path, "
            "relative_path_key, filename, size_bytes, first_seen_at, last_seen_at, "
            "status, presence_status, health_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'active','present','ok')",
            (str(uuid.uuid4()), row["photo_id"], row["library_id"], "/x/d.jpg",
             "a.jpg", row["relative_path_key"], "d.jpg", 1, "t", "t"),
        )


def test_deleted_then_recreated_path_does_not_violate_uniqueness(tmp_path):
    # A missing File keeps its identity; a genuinely different photo may later
    # occupy the same relative path. Present-scoped uniqueness must allow this.
    lib = tmp_path / "lib"; _img(lib / "a.jpg", "red")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    (lib / "a.jpg").unlink(); scan_library(conn, lib)          # -> missing
    _img(lib / "a.jpg", "blue"); scan_library(conn, lib)       # new content, same path
    present = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE presence_status='present'"
    ).fetchone()["n"]
    assert present == 1                                        # no crash, one present File


def test_archive_inside_library_rejected(tmp_path):
    from ppa.scanner import ArchiveInsideLibraryError
    lib = tmp_path / "Photos"; _img(lib / "orig.jpg")
    (lib / ".ppa").mkdir()
    conn = connect(tmp_path / "c.sqlite3")
    with pytest.raises(ArchiveInsideLibraryError):
        scan_library(conn, lib, protected_paths=[lib / ".ppa" / "catalogue.sqlite3",
                                                 lib / ".ppa" / "thumbnails"])


def test_current_vs_historical_mismatch_counts(tmp_path):
    from ppa.integrity import verify_library
    from ppa.catalogue import library_stats
    lib = tmp_path / "lib"; p = lib / "a.jpg"; _img(p, "red")
    original = p.read_bytes()                 # keep exact good bytes
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)

    _img(p, "blue")                           # silent content change
    verify_library(conn)                      # flags hash_mismatch
    s1 = library_stats(conn)
    assert s1.hash_mismatches == 1            # CURRENT state: one file mismatched
    assert s1.historical_mismatch_events == 1  # and one event in the ledger

    p.write_bytes(original)                   # restore the exact original bytes
    verify_library(conn)                      # health returns to ok
    s2 = library_stats(conn)
    assert s2.hash_mismatches == 0            # dashboard reflects current health
    assert s2.historical_mismatch_events == 1  # ledger still remembers it happened
