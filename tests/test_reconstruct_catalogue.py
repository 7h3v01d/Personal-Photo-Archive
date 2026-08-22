"""Phase 7.1 — reconstruction persistence, sticky decisions, and flow.

Read-of-evidence only: reconstruction writes only to its own table, never to
observations or the recorded date. Human decisions are sticky across re-runs.
"""

from __future__ import annotations

from pathlib import Path

from PIL import ExifTags, Image

from ppa import anchors, catalogue, metadata
from ppa.db import connect
from ppa.reconstruct_catalogue import (
    analyse_library_reconstructed,
    confirm_reconstruction,
    list_reconstructions,
    reject_reconstruction,
    store_reconstructions,
)
from ppa.scanner import scan_library


def _jpg(p: Path, dto: str, make="Canon", model="5D", serial="SN-1"):
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (32, 24), "red")
    ex = im.getexif(); ex[0x010F] = make; ex[0x0110] = model
    sub = ex.get_ifd(ExifTags.IFD.Exif); sub[0x9003] = dto
    if serial:
        sub[0xA431] = serial
    im.save(p, format="JPEG", exif=ex)


def _reset_run(tmp_path, n=5):
    lib = tmp_path / "lib"
    for i in range(n):
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*5:02d}:00")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    return conn


def _fid(conn, name):
    return conn.execute("SELECT id FROM files WHERE filename = ?", (name,)).fetchone()["id"]


def test_migration_creates_reconstructions_table(tmp_path):
    conn = connect(tmp_path / "c.sqlite3")
    assert conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] >= 10
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(reconstructions)")]
    assert {"file_id", "start_date", "end_date", "confidence", "method", "status"} <= set(cols)


def test_offset_run_proposals_stored(tmp_path):
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    counts = store_reconstructions(conn)
    assert counts["proposed"] == 5
    rows = {r.file_id: r for r in list_reconstructions(conn)}
    anchored = rows[_fid(conn, "IMG_0203.jpg")]
    assert anchored.method == "direct" and str(anchored.start_date) == "2004-12-25"
    others = [r for fid, r in rows.items() if fid != _fid(conn, "IMG_0203.jpg")]
    assert all(r.method == "offset" and str(r.start_date) == "2004-12-25" for r in others)


def test_decisions_are_sticky_across_reruns(tmp_path):
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    store_reconstructions(conn)
    confirm_reconstruction(conn, _fid(conn, "IMG_0203.jpg"))
    reject_reconstruction(conn, _fid(conn, "IMG_0201.jpg"))

    counts = store_reconstructions(conn)              # re-run
    assert counts["skipped_decided"] == 2
    by = {r.file_id: r for r in list_reconstructions(conn)}
    assert by[_fid(conn, "IMG_0203.jpg")].status == "confirmed"
    assert by[_fid(conn, "IMG_0201.jpg")].status == "rejected"
    assert by[_fid(conn, "IMG_0203.jpg")].decided_at is not None


def test_confirm_and_reject_return_false_without_row(tmp_path):
    conn = _reset_run(tmp_path)
    assert confirm_reconstruction(conn, "no-such-file") is False
    assert reject_reconstruction(conn, "no-such-file") is False


def test_reconstruction_is_read_only_wrt_observations(tmp_path):
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    before = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    rev = conn.execute("SELECT current_revision_id FROM files LIMIT 1").fetchone()[0]
    store_reconstructions(conn)
    analyse_library_reconstructed(conn)
    after = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    rev2 = conn.execute("SELECT current_revision_id FROM files LIMIT 1").fetchone()[0]
    assert after == before and rev == rev2
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_list_filters_by_status(tmp_path):
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    store_reconstructions(conn)
    confirm_reconstruction(conn, _fid(conn, "IMG_0203.jpg"))
    assert len(list_reconstructions(conn, status="confirmed")) == 1
    assert len(list_reconstructions(conn, status="proposed")) == 4


def test_forgetting_files_cascades_to_reconstructions(tmp_path):
    # reconstructions.file_id has ON DELETE CASCADE, so removing a library's files
    # cleans its reconstructions too.
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    store_reconstructions(conn)
    lid = catalogue.list_libraries(conn)[0].id
    catalogue.forget_library(conn, lid)
    assert list_reconstructions(conn) == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
