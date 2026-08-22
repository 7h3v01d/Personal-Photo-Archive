from __future__ import annotations

from pathlib import Path

from PIL import ExifTags, Image
import pytest

from ppa import anchors, metadata
from ppa.db import connect
from ppa.reconstruct_catalogue import confirm_reconstruction, store_reconstructions
from ppa.review_queue import QUEUE_SCHEMA, build_review_queue
from ppa.scanner import scan_library


def _reset_run(tmp_path: Path, *, n: int = 12):
    lib = tmp_path / "library"
    lib.mkdir()
    for i in range(n):
        p = lib / f"IMG_{200+i:04d}.jpg"
        im = Image.new("RGB", (32, 24), "red")
        ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
        sub = ex.get_ifd(ExifTags.IFD.Exif)
        sub[0x9003] = f"2001:01:01 00:{i:02d}:00"
        sub[0xA431] = "SN-Q-1"
        im.save(p, format="JPEG", exif=ex)
    conn = connect(tmp_path / "db.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    rows = conn.execute("SELECT id,filename FROM files ORDER BY filename").fetchall()
    return conn, {r["filename"]: r["id"] for r in rows}


def test_queue_is_read_only_deterministic_and_versioned(tmp_path):
    conn, _ = _reset_run(tmp_path)
    lid = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    before = conn.total_changes
    a = build_review_queue(conn, library_id=lid)
    middle = conn.total_changes
    b = build_review_queue(conn, library_id=lid)
    assert a.schema == QUEUE_SCHEMA
    assert a.to_json(pretty=False) == b.to_json(pretty=False)
    assert before == middle == conn.total_changes
    conn.close()


def test_high_leverage_reset_candidate_orders_before_low_value_items(tmp_path):
    conn, ids = _reset_run(tmp_path, n=12)
    lid = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    q = build_review_queue(conn, library_id=lid)
    assert q.items
    assert q.items[0].priority == "A"
    assert q.items[0].action == "HIGH_LEVERAGE_ANCHOR"
    assert q.items[0].affected_count >= 10
    assert all(i.priority != "D" for i in q.actionable())
    conn.close()


def test_current_proposal_is_actionable_and_stale_proposal_moves_to_refresh(tmp_path):
    conn, ids = _reset_run(tmp_path, n=5)
    fid = ids["IMG_0202.jpg"]
    lid = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn)
    q = build_review_queue(conn, library_id=lid)
    item = next(i for i in q.items if i.file_id == fid)
    assert item.action == "REVIEW_CURRENT_PROPOSAL" and not item.stale

    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")
    q2 = build_review_queue(conn, library_id=lid)
    item2 = next(i for i in q2.items if i.file_id == fid)
    assert item2.priority == "A"
    assert item2.action == "REFRESH_STALE_PROPOSAL" and item2.stale
    conn.close()


def test_stale_confirmed_decision_is_reopen_refresh_priority_a(tmp_path):
    conn, ids = _reset_run(tmp_path, n=5)
    fid = ids["IMG_0202.jpg"]
    lid = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")
    q = build_review_queue(conn, library_id=lid)
    item = next(i for i in q.items if i.file_id == fid)
    assert item.priority == "A"
    assert item.action == "REOPEN_REFRESH_DECISION"
    assert item.stale is True
    conn.close()


def test_explicit_scope_cannot_leak_other_library(tmp_path):
    c = connect(tmp_path / "db.sqlite3")
    for name in ("a", "b"):
        lib = tmp_path / name; lib.mkdir()
        Image.new("RGB", (10, 10), "white").save(lib / f"{name}.jpg")
        scan_library(c, lib)
    libs = c.execute("SELECT id,root_display_path FROM libraries ORDER BY id").fetchall()
    l1, l2 = libs[0]["id"], libs[1]["id"]
    foreign = c.execute("SELECT id FROM files WHERE library_id=?", (l2,)).fetchone()["id"]
    with pytest.raises(ValueError):
        build_review_queue(c, library_id=l1, file_ids=[foreign])
    c.close()


def test_empty_scope_is_valid(tmp_path):
    conn, _ = _reset_run(tmp_path, n=1)
    lid = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    q = build_review_queue(conn, library_id=lid, directory_prefix="not-here")
    assert q.total_items == 0 and q.actionable_items == 0 and q.items == ()
    conn.close()
