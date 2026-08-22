from __future__ import annotations

from pathlib import Path
import sqlite3
import pytest
from PIL import ExifTags, Image

from ppa import anchors, metadata
from ppa.batch_review import confirm_batch, plan_batch_confirmation
from ppa.db import connect
from ppa.reconstruct_catalogue import list_reconstructions, store_reconstructions
from ppa.scanner import scan_library


def _jpg(p: Path, dto: str, serial="SN-BATCH"):
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (40, 30), "red")
    ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
    sub = ex.get_ifd(ExifTags.IFD.Exif); sub[0x9003] = dto; sub[0xA431] = serial
    im.save(p, format="JPEG", exif=ex)


def _setup(tmp_path, n=5):
    lib = tmp_path / "lib"
    for i in range(n):
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*5:02d}:00")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM files")}
    anchors.add_anchor(conn, "file", ids["IMG_0203.jpg"], "exact", "2004-12-25")
    store_reconstructions(conn)
    return conn, ids


def test_plan_is_strict_traceable_and_samples_run(tmp_path):
    conn, ids = _setup(tmp_path, 9)
    before = conn.total_changes
    plan = plan_batch_confirmation(conn, ids["IMG_0201.jpg"])
    assert plan is not None and plan.member_count == 9
    assert plan.anchor_file_ids == (ids["IMG_0203.jpg"],)
    assert plan.day_offset == 1454
    assert len(plan.sample_file_ids) == 5
    assert plan.sample_file_ids[0] == plan.members[0].file_id
    assert plan.sample_file_ids[-1] == plan.members[-1].file_id
    assert conn.total_changes == before


def test_batch_commit_confirms_all_atomically(tmp_path):
    conn, ids = _setup(tmp_path)
    plan = plan_batch_confirmation(conn, ids["IMG_0201.jpg"])
    assert plan is not None
    assert confirm_batch(conn, plan) == 5
    rows = list_reconstructions(conn)
    assert len(rows) == 5 and all(r.status == "confirmed" for r in rows)


def test_changed_evidence_aborts_without_partial_decisions(tmp_path):
    conn, ids = _setup(tmp_path)
    plan = plan_batch_confirmation(conn, ids["IMG_0201.jpg"])
    assert plan is not None
    anchors.add_anchor(conn, "file", ids["IMG_0203.jpg"], "exact", "2005-12-25")
    with pytest.raises(ValueError, match="batch changed"):
        confirm_batch(conn, plan)
    states = {r.status for r in list_reconstructions(conn)}
    assert states == {"proposed"}


def test_decided_member_makes_whole_group_ineligible(tmp_path):
    conn, ids = _setup(tmp_path)
    conn.execute("UPDATE reconstructions SET status='confirmed' WHERE file_id=?", (ids["IMG_0201.jpg"],))
    conn.commit()
    assert plan_batch_confirmation(conn, ids["IMG_0202.jpg"]) is None


def test_range_or_weak_group_cannot_batch(tmp_path):
    conn, ids = _setup(tmp_path)
    conn.execute("UPDATE reconstructions SET end_date='2004-12-26' WHERE file_id=?", (ids["IMG_0201.jpg"],))
    conn.commit()
    assert plan_batch_confirmation(conn, ids["IMG_0202.jpg"]) is None


def test_model_only_camera_group_never_batch_eligible(tmp_path):
    lib = tmp_path / "lib"
    for i in range(5):
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*5:02d}:00", serial="00000000")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    fid = conn.execute("SELECT id FROM files LIMIT 1").fetchone()["id"]
    assert plan_batch_confirmation(conn, fid) is None

def test_batch_confirmation_never_writes_source_photos(tmp_path):
    conn, ids = _setup(tmp_path)
    paths = [Path(r["path"]) for r in conn.execute("SELECT path FROM files ORDER BY id")]
    before = {p: p.read_bytes() for p in paths}
    plan = plan_batch_confirmation(conn, ids["IMG_0201.jpg"])
    assert plan is not None
    confirm_batch(conn, plan)
    assert {p: p.read_bytes() for p in paths} == before
