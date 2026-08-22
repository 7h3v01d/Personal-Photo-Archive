from __future__ import annotations

from pathlib import Path
from PIL import ExifTags, Image

from ppa import anchors, metadata
from ppa.anchor_opportunities import QUESTION_SCHEMA, build_anchor_questions
from ppa.db import connect
from ppa.scanner import scan_library


def _reset_run(tmp_path: Path, *, n: int = 12):
    lib = tmp_path / "library"; lib.mkdir()
    for i in range(n):
        p = lib / f"IMG_{200+i:04d}.jpg"
        im = Image.new("RGB", (24, 18), "navy")
        ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
        sub = ex.get_ifd(ExifTags.IFD.Exif)
        sub[0x9003] = f"2001:01:01 00:{i:02d}:00"; sub[0xA431] = "SN-723-1"
        im.save(p, format="JPEG", exif=ex)
    conn = connect(tmp_path / "db.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    lid = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id,filename FROM files")}
    return conn, lid, ids


def test_best_question_is_deterministic_traceable_and_read_only(tmp_path):
    conn, lid, ids = _reset_run(tmp_path)
    before = conn.total_changes
    a = build_anchor_questions(conn, library_id=lid)
    middle = conn.total_changes
    b = build_anchor_questions(conn, library_id=lid)
    assert a.schema == QUESTION_SCHEMA
    assert a.to_json(pretty=False) == b.to_json(pretty=False)
    assert before == middle == conn.total_changes
    assert a.best is not None
    assert a.best.affected_count >= 10
    assert a.best.file_id not in a.best.affected_file_ids
    assert set(a.best.affected_file_ids).issubset(set(a.best.group_file_ids))
    conn.close()


def test_exact_human_anchor_means_do_not_ask_group_for_another_exact_anchor(tmp_path):
    conn, lid, ids = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", ids["IMG_0203.jpg"], "exact", "2004-12-25")
    qs = build_anchor_questions(conn, library_id=lid)
    assert qs.questions == ()
    conn.close()


def test_range_anchor_does_not_hide_exact_date_opportunity(tmp_path):
    conn, lid, ids = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", ids["IMG_0203.jpg"], "range", "2004-12-20", "2004-12-30")
    qs = build_anchor_questions(conn, library_id=lid)
    assert qs.best is not None
    assert qs.best.file_id != ids["IMG_0203.jpg"]
    conn.close()


def test_model_only_camera_is_not_presented_as_high_leverage_question(tmp_path):
    lib = tmp_path / "library"; lib.mkdir()
    for i in range(12):
        p = lib / f"IMG_{300+i:04d}.jpg"
        im = Image.new("RGB", (24, 18), "white")
        ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
        sub = ex.get_ifd(ExifTags.IFD.Exif); sub[0x9003] = f"2001:01:01 00:{i:02d}:00"
        im.save(p, format="JPEG", exif=ex)
    conn = connect(tmp_path / "db.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    lid = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    assert build_anchor_questions(conn, library_id=lid).questions == ()
    conn.close()
