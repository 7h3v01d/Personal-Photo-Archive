"""Hardening 3.2.3 — identity-boundary regressions.

Two acceptance items (same-path restoration; library containment) plus the
state-recovery and startup-selector fixes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from ppa import catalogue
from ppa.db import connect
from ppa.integrity import verify_library
from ppa.scanner import scan_library


def _img(p: Path, color="red") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(p)


def test_missing_then_restored_same_path_reuses_file_id(tmp_path):
    lib = tmp_path / "lib"; p = lib / "a.png"; _img(p, "red")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    original = p.read_bytes()
    file_id = conn.execute("SELECT id FROM files").fetchone()["id"]

    p.unlink(); scan_library(conn, lib)               # -> missing
    p.write_bytes(original); rep = scan_library(conn, lib)  # exact bytes return

    rows = conn.execute("SELECT id, presence_status FROM files").fetchall()
    assert len(rows) == 1                              # one physical file, one File row
    assert rows[0]["id"] == file_id                    # SAME File reused
    assert rows[0]["presence_status"] == "present"
    assert rep.restored_files == 1
    assert rep.duplicate_files == 0


def test_file_symlink_outside_library_is_not_catalogued(tmp_path):
    lib = tmp_path / "library"
    _img(lib / "real.jpg", "green")
    outside = tmp_path / "outside.jpg"; _img(outside, "blue")
    try:
        os.symlink(outside, lib / "linked.jpg")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    conn = connect(tmp_path / "c.sqlite3")
    rep = scan_library(conn, lib)

    paths = [r["path"] for r in conn.execute("SELECT path FROM files")]
    assert len(paths) == 1                             # only the real in-library file
    assert paths[0] == os.path.realpath(str(lib / "real.jpg"))
    assert rep.external_skipped == 1
    # Invariant: every catalogued File resolves beneath the library root.
    root = os.path.realpath(str(lib))
    for pth in paths:
        assert os.path.commonpath([root, pth]) == root


def test_library_state_recovers_active_after_successful_scan(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)

    os.rename(lib, tmp_path / "lib_off")               # take offline
    verify_library(conn)
    assert conn.execute("SELECT state FROM libraries").fetchone()["state"] == "unavailable"

    os.rename(tmp_path / "lib_off", lib)               # reconnect
    scan_library(conn, lib)                            # strong evidence it's back
    assert conn.execute("SELECT state FROM libraries").fetchone()["state"] == "active"


def test_startup_library_selector_is_absolute_after_relative_scan(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    monkeypatch.chdir(tmp_path)
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, Path("lib"))                    # scanned via relative spelling

    stats = catalogue.library_stats(conn)
    assert stats.last_library_path is not None
    # The startup selector must be absolute (not the raw relative "lib"), so the
    # GUI reopens the right directory from any working directory.
    assert os.path.isabs(stats.last_library_path)


def test_exact_duplicate_still_detected(tmp_path):
    # Guard: the present-twin tightening must not break real duplicates.
    lib = tmp_path / "lib"; _img(lib / "a.jpg", "red")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    (lib / "copy.jpg").write_bytes((lib / "a.jpg").read_bytes())
    rep = scan_library(conn, lib)
    assert rep.duplicate_files == 1
    photos = conn.execute("SELECT COUNT(DISTINCT photo_id) AS n FROM files").fetchone()["n"]
    assert photos == 1                                 # both files -> one Photo
