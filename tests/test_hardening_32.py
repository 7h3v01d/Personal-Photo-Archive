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
