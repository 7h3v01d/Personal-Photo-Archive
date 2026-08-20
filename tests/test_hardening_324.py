"""Hardening 3.2.4 — operational boundary regressions (final closure)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from ppa import catalogue
from ppa.db import connect
from ppa.integrity import verify_library
from ppa.scanner import scan_library, LibraryUnavailableError


def _img(p: Path, color="red") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(p)


def test_unavailable_root_scan_does_not_mark_active(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)

    os.rename(lib, tmp_path / "gone")          # whole root disappears
    rep = scan_library(conn, lib)              # scan the now-absent root

    assert rep.library_unavailable is True
    assert conn.execute("SELECT state FROM libraries").fetchone()["state"] == "unavailable"
    # Photographs are NOT concluded missing from an unreachable root.
    assert conn.execute("SELECT presence_status FROM files").fetchone()["presence_status"] == "present"

    os.rename(tmp_path / "gone", lib)          # reconnect
    scan_library(conn, lib)
    assert conn.execute("SELECT state FROM libraries").fetchone()["state"] == "active"


def test_nonexistent_root_creates_no_library(tmp_path):
    conn = connect(tmp_path / "c.sqlite3")
    with pytest.raises(LibraryUnavailableError):
        scan_library(conn, tmp_path / "never" / "existed")
    assert conn.execute("SELECT COUNT(*) AS n FROM libraries").fetchone()["n"] == 0


def test_internal_symlink_alias_does_not_abort_scan(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "real.jpg", "green")
    try:
        os.symlink(lib / "real.jpg", lib / "link.jpg")  # alias to an in-library file
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    conn = connect(tmp_path / "c.sqlite3")
    rep = scan_library(conn, lib)              # must NOT raise IntegrityError

    assert conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 1
    assert rep.alias_skipped == 1


def test_duplicate_files_counts_only_present_copies(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg", "red")
    (lib / "b.jpg").write_bytes((lib / "a.jpg").read_bytes())  # genuine copy
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    assert catalogue.library_stats(conn).duplicate_files == 2   # two present copies

    (lib / "b.jpg").unlink(); scan_library(conn, lib)           # one copy disappears
    # Only one copy currently exists, so the dashboard shows no current duplicate.
    assert catalogue.library_stats(conn).duplicate_files == 0
