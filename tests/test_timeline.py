from __future__ import annotations

from pathlib import Path
from PIL import ExifTags, Image
import pytest

from ppa import anchors, metadata
from ppa.db import connect
from ppa.reconstruct_catalogue import confirm_reconstruction, reject_reconstruction, store_reconstructions
from ppa.scanner import scan_library
from ppa.timeline import TIMELINE_SCHEMA, build_timeline


def _jpg(path: Path, dto: str, *, serial="SN-TIMELINE", color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (32, 24), color)
    ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
    sub = ex.get_ifd(ExifTags.IFD.Exif); sub[0x9003] = dto; sub[0xA431] = serial
    im.save(path, format="JPEG", exif=ex)


def _library(tmp_path: Path):
    lib = tmp_path / "lib"
    _jpg(lib / "good.jpg", "2019:06:12 10:00:00")
    for i in range(5):
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*5:02d}:00")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    return conn, lib, lid


def _fid(conn, name):
    return conn.execute("SELECT id FROM files WHERE filename=?", (name,)).fetchone()[0]


def test_timeline_is_read_only_and_clean_recorded_date_is_placed(tmp_path):
    conn, _, lid = _library(tmp_path)
    before = conn.total_changes
    view = build_timeline(conn, library_id=lid, generated_at="fixed")
    assert conn.total_changes == before
    assert view.schema == TIMELINE_SCHEMA and view.read_only
    item = next(i for i in view.items if i.filename == "good.jpg")
    assert item.lane == "placed" and item.source == "reconciled"
    assert item.start_date == "2019-06-12"


def test_fresh_confirmed_reconstruction_beats_questionable_recorded_date(tmp_path):
    conn, _, lid = _library(tmp_path)
    fid = _fid(conn, "IMG_0203.jpg")
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    view = build_timeline(conn, library_id=lid, generated_at="fixed")
    item = next(i for i in view.items if i.file_id == fid)
    assert item.lane == "placed"
    assert item.source == "confirmed_reconstruction"
    assert item.start_date == "2004-12-25"


def test_fresh_proposal_is_tentative_not_authoritative(tmp_path):
    conn, _, lid = _library(tmp_path)
    fid = _fid(conn, "IMG_0203.jpg")
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn)
    target = _fid(conn, "IMG_0201.jpg")
    item = next(i for i in build_timeline(conn, library_id=lid, generated_at="fixed").items
                if i.file_id == target)
    assert item.lane == "tentative"
    assert item.source == "proposed_reconstruction"


def test_stale_confirmed_reconstruction_is_never_timeline_authority(tmp_path):
    conn, _, lid = _library(tmp_path)
    fid = _fid(conn, "IMG_0203.jpg")
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")
    item = next(i for i in build_timeline(conn, library_id=lid, generated_at="fixed").items
                if i.file_id == fid)
    # The current exact human anchor can independently make the reconciled date
    # TRUSTED, but the stale *old reconstruction* itself is never used as authority.
    assert item.source != "confirmed_reconstruction"
    assert item.start_date != "2004-12-25"


def test_rejected_reconstruction_can_fall_back_to_clean_recorded_chronology(tmp_path):
    conn, lib, lid = _library(tmp_path)
    # Give a clean photo a direct proposal, reject it, then ensure the reliable
    # recorded chronology remains usable rather than being poisoned by rejection.
    fid = _fid(conn, "good.jpg")
    anchors.add_anchor(conn, "file", fid, "exact", "2019-06-12")
    store_reconstructions(conn); reject_reconstruction(conn, fid)
    item = next(i for i in build_timeline(conn, library_id=lid, generated_at="fixed").items
                if i.file_id == fid)
    assert item.lane == "placed" and item.source == "reconciled"


def test_questionable_without_safe_replacement_is_unplaced(tmp_path):
    conn, _, lid = _library(tmp_path)
    item = next(i for i in build_timeline(conn, library_id=lid, generated_at="fixed").items
                if i.filename == "IMG_0201.jpg")
    assert item.lane == "unplaced" and item.start_date is None


def test_scope_isolation_and_unknown_library_fail_closed(tmp_path):
    conn, lib, lid = _library(tmp_path)
    only = _fid(conn, "good.jpg")
    view = build_timeline(conn, library_id=lid, file_ids=[only], generated_at="fixed")
    assert [i.file_id for i in view.items] == [only]
    with pytest.raises(ValueError):
        build_timeline(conn, library_id=999, generated_at="fixed")


def test_json_is_deterministic_except_generation_time(tmp_path):
    conn, _, lid = _library(tmp_path)
    a = build_timeline(conn, library_id=lid, generated_at="A").to_dict()
    b = build_timeline(conn, library_id=lid, generated_at="B").to_dict()
    a["generated_at"] = b["generated_at"] = "X"
    assert a == b


def test_confirmed_range_remains_range_not_fake_point_date(tmp_path):
    conn, _, lid = _library(tmp_path)
    fid = _fid(conn, "IMG_0203.jpg")
    anchors.add_anchor(conn, "file", fid, "range", "2004-12-24", "2004-12-27")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    item = next(i for i in build_timeline(conn, library_id=lid, generated_at="fixed").items
                if i.file_id == fid)
    assert item.lane == "range"
    assert item.source == "confirmed_reconstruction"
    assert item.start_date == "2004-12-24" and item.end_date == "2004-12-27"
