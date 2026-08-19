"""Hardening 3.1 — provenance interface regressions.

Attacks from adversarial review at the seams between the Schema-v3 model and
the older Phase-2 code. Each must stay closed permanently.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from ppa import metadata
from ppa.db import connect
from ppa.integrity import verify_library
from ppa.scanner import scan_library


def _img(p: Path, color="red", size=(40, 30)) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(p)


def _exif_jpg(p: Path, dto: str, color="red") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (40, 30), color)
    exif = img.getexif()
    exif[0x010F] = "Canon"; exif[0x0110] = "A70"
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    sub[0x9003] = dto
    img.save(p, format="JPEG", exif=exif)


# --- integrity.py on the v3 model -------------------------------------------


def test_verify_missing_sets_presence_not_just_status(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    (lib / "a.jpg").unlink()
    verify_library(conn)
    r = conn.execute("SELECT status, presence_status FROM files").fetchone()
    assert r["presence_status"] == "missing"  # no more status/presence conflict
    assert r["status"] == "missing"


def test_verify_hash_mismatch_sets_health(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg", "red")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    _img(lib / "a.jpg", "blue")  # silent content change
    rep = verify_library(conn)
    assert rep.mismatches == 1
    r = conn.execute("SELECT presence_status, health_status FROM files").fetchone()
    assert r["presence_status"] == "present"
    assert r["health_status"] == "hash_mismatch"  # not "ok"


def test_verify_backfill_updates_revision_not_only_file(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    conn.execute("UPDATE files SET sha256 = NULL")
    conn.execute("UPDATE file_revisions SET sha256 = NULL")
    conn.commit()
    verify_library(conn)
    file_sha = conn.execute("SELECT sha256 FROM files").fetchone()["sha256"]
    rev_sha = conn.execute("SELECT sha256 FROM file_revisions").fetchone()["sha256"]
    assert file_sha and rev_sha and file_sha == rev_sha  # no revision drift


def test_flagged_mismatch_poisons_scan_fast_path(tmp_path):
    # After Verify flags a mismatch, a scan must NOT trust the size+mtime
    # fast-path, and must NOT silently promote the suspicious bytes to a new
    # trusted revision. The recorded revision is held for reconciliation.
    lib = tmp_path / "lib"; p = lib / "a.png"; _img(p, "red")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    st = os.stat(p)
    orig_sha = conn.execute("SELECT sha256 FROM files").fetchone()["sha256"]
    orig_rev = conn.execute("SELECT current_revision_id FROM files").fetchone()["current_revision_id"]

    _img(p, "blue")                       # different bytes
    os.utime(p, (st.st_atime, st.st_mtime))  # restore mtime (defeat naive fast-path)
    verify_library(conn)                  # flags hash_mismatch
    scan_library(conn, lib)               # must not launder the mismatch away

    r = conn.execute("SELECT sha256, current_revision_id, health_status FROM files").fetchone()
    assert r["health_status"] == "hash_mismatch"
    assert r["sha256"] == orig_sha
    assert r["current_revision_id"] == orig_rev


# --- provenance immutability -------------------------------------------------


def test_cannot_reextract_historical_revision_from_current_bytes(tmp_path):
    lib = tmp_path / "lib"; p = lib / "a.jpg"
    _exif_jpg(p, "2001:01:01 00:00:00")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib); metadata.extract_stale(conn)
    rev1 = conn.execute("SELECT current_revision_id FROM files").fetchone()["current_revision_id"]

    _exif_jpg(p, "2004:12:25 09:14:32")
    scan_library(conn, lib); metadata.extract_stale(conn)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]

    status = metadata.extract_for_revision(conn, fid, rev1)  # attack
    assert status == "refused_not_current"
    v = conn.execute(
        "SELECT value FROM metadata_observations WHERE file_revision_id=? AND key='DateTimeOriginal'",
        (rev1,),
    ).fetchone()
    assert v["value"] == "2001:01:01 00:00:00"  # history intact


def test_filesystem_observation_history_survives_revision(tmp_path):
    lib = tmp_path / "lib"; p = lib / "a.jpg"
    _exif_jpg(p, "2001:01:01 00:00:00")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib); metadata.extract_stale(conn)
    rev1 = conn.execute("SELECT current_revision_id FROM files").fetchone()["current_revision_id"]

    _exif_jpg(p, "2004:12:25 09:14:32")   # content change -> revision 2
    scan_library(conn, lib); metadata.extract_stale(conn)

    rev1_fs = conn.execute(
        "SELECT value FROM metadata_observations "
        "WHERE file_revision_id=? AND source='filesystem' AND key='mtime'", (rev1,)
    ).fetchone()
    assert rev1_fs is not None  # revision 1's filesystem-date evidence survives


# --- scan session audit ------------------------------------------------------


def test_crashed_scan_records_failed(tmp_path, monkeypatch):
    lib = tmp_path / "lib"; _img(lib / "a.jpg")
    conn = connect(tmp_path / "c.sqlite3")

    import ppa.scanner as scanner_mod

    def boom(*a, **k):
        raise RuntimeError("inventory exploded")

    monkeypatch.setattr(scanner_mod, "_inventory_supported_file", boom)
    with pytest.raises(RuntimeError):
        scan_library(conn, lib)

    sess = conn.execute(
        "SELECT scan_status FROM import_sessions ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert sess["scan_status"] == "failed"  # audit ledger is honest


# --- DB ownership invariants -------------------------------------------------


def test_db_rejects_cross_file_current_revision(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg"); _img(lib / "b.jpg", "blue")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    rows = conn.execute("SELECT id, current_revision_id FROM files ORDER BY filename").fetchall()
    a, b = rows[0], rows[1]
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE files SET current_revision_id = ? WHERE id = ?",
                     (b["current_revision_id"], a["id"]))


def test_db_rejects_cross_owner_observation(tmp_path):
    lib = tmp_path / "lib"; _img(lib / "a.jpg"); _img(lib / "b.jpg", "blue")
    conn = connect(tmp_path / "c.sqlite3"); scan_library(conn, lib)
    a = conn.execute("SELECT id FROM files WHERE filename='a.jpg'").fetchone()
    b_rev = conn.execute(
        "SELECT current_revision_id FROM files WHERE filename='b.jpg'"
    ).fetchone()["current_revision_id"]
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO metadata_observations (file_id, file_revision_id, source, key, value) "
            "VALUES (?, ?, 'exif', 'k', 'v')",
            (a["id"], b_rev),
        )


# --- Windows canonicalisation ------------------------------------------------


def test_library_canonical_key_is_case_folded(tmp_path):
    from ppa.scanner import _resolve_library
    conn = connect(tmp_path / "c.sqlite3")
    (tmp_path / "Lib").mkdir()
    lid1, canon1 = _resolve_library(conn, tmp_path / "Lib")
    # normcase folds case on Windows; on POSIX it's a no-op but realpath still
    # normalises. The same directory must resolve to the same canonical key.
    lid2, canon2 = _resolve_library(conn, tmp_path / "Lib")
    assert lid1 == lid2 and canon1 == canon2
    assert canon1 == os.path.normcase(os.path.realpath(str(tmp_path / "Lib")))
