"""Adversarial regression tests for the Schema-v2 archive-hardening pass.

Each scenario below was reproduced against the current build and reflects a
real provenance/correctness defect found in adversarial review. They are
marked xfail(strict=True): they fail today, and the moment a fix makes one
pass, strict mode turns that into a hard failure so the xfail marker must be
removed. That keeps the spec and the code honest with each other.

Do NOT relax these into ordinary skips. They are the acceptance criteria for
declaring Phase 3 / archive-core hardening complete.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from ppa import metadata
from ppa.db import connect
from ppa.scanner import scan_library

strict_xfail = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="pending Schema-v2 hardening",
)


def _img(path: Path, color="red", size=(40, 30)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def _jpeg_with_exif(path: Path, *, model="PowerShot A70", dto="2004:12:25 09:14:32",
                    color="red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (60, 40), color)
    exif = img.getexif()
    exif[0x010F] = "Canon"
    exif[0x0110] = model
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    sub[0x9003] = dto
    img.save(path, format="JPEG", exif=exif)


# --- Scanner / multi-library ------------------------------------------------


def test_two_libraries_keep_both_active(tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    _img(a / "a.jpg", "red")
    _img(b / "b.jpg", "blue")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, a)
    scan_library(conn, b)
    status = conn.execute("SELECT status FROM files WHERE filename='a.jpg'").fetchone()["status"]
    assert status == "active"  # a.jpg still exists in library A


def test_identical_content_across_libraries_kept_separate(tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    _img(a / "a.jpg", "green")
    (b).mkdir(parents=True, exist_ok=True)
    Image.open(a / "a.jpg").save(b / "b.jpg")  # byte-identical copy
    # actually copy raw bytes to guarantee identity
    (b / "b.jpg").write_bytes((a / "a.jpg").read_bytes())
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, a)
    scan_library(conn, b)
    rows = conn.execute("SELECT filename FROM files WHERE status='active'").fetchall()
    names = {r["filename"] for r in rows}
    assert names == {"a.jpg", "b.jpg"}  # two real archive copies, not one


def test_present_but_corrupt_is_not_missing(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    img = lib / "a.jpg"
    _img(img)
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, lib)
    img.write_bytes(b"not a valid image anymore")  # present, unreadable
    scan_library(conn, lib)
    status = conn.execute("SELECT status FROM files").fetchone()["status"]
    assert status != "missing"  # it is present-but-unreadable, not gone


def test_full_previous_sha_is_preserved_on_content_change(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    img = lib / "a.jpg"
    _img(img, "red")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, lib)
    old_sha = conn.execute("SELECT sha256 FROM files").fetchone()["sha256"]

    _img(img, "blue")  # external edit in place
    scan_library(conn, lib)

    def preserved(sha: str) -> bool:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM integrity_events WHERE detail LIKE ?",
            (f"%{sha}%",),
        ).fetchone()["n"]
        if n:
            return True
        try:
            m = conn.execute(
                "SELECT COUNT(*) AS n FROM file_revisions WHERE sha256 = ?", (sha,)
            ).fetchone()["n"]
            return m > 0
        except sqlite3.OperationalError:
            return False

    assert preserved(old_sha)  # the archive must not forget what those bytes were


def test_stale_hash_index_keeps_distinct_content_distinct(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _img(lib / "a.jpg", "red")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, lib)
    red = (lib / "a.jpg").read_bytes()

    _img(lib / "a.jpg", "blue")        # a -> BLUE content
    (lib / "b.jpg").write_bytes(red)   # b -> old RED content
    scan_library(conn, lib)

    rows = conn.execute("SELECT filename, photo_id, sha256 FROM files ORDER BY filename").fetchall()
    photo_ids = {r["photo_id"] for r in rows}
    shas = {r["sha256"] for r in rows}
    assert len(shas) == 2           # genuinely different content
    assert len(photo_ids) == 2      # therefore genuinely different Photos


def test_incomplete_traversal_does_not_mark_missing(tmp_path: Path, monkeypatch) -> None:
    lib = tmp_path / "lib"
    _img(lib / "top.jpg", "red")
    _img(lib / "sub" / "deep.jpg", "blue")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, lib)

    # Simulate an unreadable subdirectory: os.walk invokes its onerror callback
    # and yields nothing for 'sub'. A fail-closed scanner must treat the scan
    # as incomplete and refuse to conclude the file there vanished.
    import ppa.scanner as scanner_mod
    real_walk = os.walk

    def failing_walk(top, *args, **kwargs):
        onerror = kwargs.get("onerror")
        for root, dirs, files in real_walk(top):
            if os.path.basename(root) == "sub":
                if onerror is not None:
                    onerror(OSError("Permission denied: sub"))
                continue
            yield root, [d for d in dirs if d != "sub"], files

    monkeypatch.setattr(scanner_mod.os, "walk", failing_walk)
    scan_library(conn, lib)
    status = conn.execute(
        "SELECT status FROM files WHERE filename='deep.jpg'"
    ).fetchone()["status"]
    assert status != "missing"


# --- Metadata provenance ----------------------------------------------------


def test_filesystem_mtime_observation_tracks_file(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    img = lib / "a.jpg"
    _jpeg_with_exif(img)
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, lib)
    metadata.extract_stale(conn)

    # Touch mtime only — no content change.
    os.utime(img, (100000, 100000))
    scan_library(conn, lib)
    metadata.extract_stale(conn)

    file_mtime = conn.execute("SELECT fs_mtime FROM files").fetchone()["fs_mtime"]
    obs_mtime = conn.execute(
        "SELECT value FROM metadata_observations WHERE source='filesystem' AND key='mtime'"
    ).fetchone()["value"]
    assert obs_mtime == file_mtime  # evidence input must not go stale


def test_transient_metadata_failure_is_retried(tmp_path: Path, monkeypatch) -> None:
    lib = tmp_path / "lib"
    _jpeg_with_exif(lib / "a.jpg")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, lib)

    real = metadata.extract_observations
    calls = {"n": 0}

    def flaky(path):
        if calls["n"] == 0:
            calls["n"] += 1
            raise OSError("transient lock")
        return real(path)

    monkeypatch.setattr(metadata, "extract_observations", flaky)
    metadata.extract_stale(conn)                       # transient failure
    monkeypatch.setattr(metadata, "extract_observations", real)
    metadata.extract_stale(conn)                       # must retry, not skip

    dto = conn.execute(
        "SELECT value FROM metadata_observations WHERE key='DateTimeOriginal'"
    ).fetchone()
    assert dto is not None


def test_camera_id_cleared_when_content_loses_exif(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    img = lib / "a.jpg"
    _jpeg_with_exif(img)
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, lib)
    metadata.extract_stale(conn)
    assert conn.execute("SELECT camera_id FROM files").fetchone()["camera_id"] is not None

    _img(img, "grey")  # replace with an image that has no EXIF
    scan_library(conn, lib)
    metadata.extract_stale(conn)
    cam = conn.execute("SELECT camera_id FROM files").fetchone()["camera_id"]
    assert cam is None  # bytes have no camera metadata -> no camera claim


def test_metadata_history_preserved_across_revisions(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    img = lib / "a.jpg"
    _jpeg_with_exif(img, dto="2001:01:01 00:00:00")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, lib)
    metadata.extract_stale(conn)

    _jpeg_with_exif(img, dto="2004:12:25 09:14:32")  # re-encoded, new date
    scan_library(conn, lib)
    metadata.extract_stale(conn)

    dates = conn.execute(
        "SELECT DISTINCT value FROM metadata_observations WHERE key='DateTimeOriginal'"
    ).fetchall()
    values = {d["value"] for d in dates}
    # Both the historical and current observed capture dates must survive.
    assert {"2001:01:01 00:00:00", "2004:12:25 09:14:32"} <= values
