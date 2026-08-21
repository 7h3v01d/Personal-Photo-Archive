"""Phase 6 Slice 3.1 — reconciliation wired into the catalogue.

DB-level: anchors table + resolution, GPS reader, manufacture floors, and the
full Slice 1->2->3 read-only assessment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from ppa import anchors, catalogue, metadata
from ppa.camera_floors import CameraFloors
from ppa.dating import Reliability
from ppa.db import connect
from ppa.reconcile import analyse_library_reconciled
from ppa.scanner import scan_library

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _jpg(p: Path, dto: str, make=None, model=None, gps=None, color="red", serial=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (32, 24), color)
    ex = im.getexif()
    if make: ex[0x010F] = make
    if model: ex[0x0110] = model
    sub = ex.get_ifd(ExifTags.IFD.Exif)
    sub[0x9003] = dto
    if serial: sub[0xA431] = serial                          # BodySerialNumber
    if gps: ex.get_ifd(ExifTags.IFD.GPSInfo)[0x001D] = gps   # GPSDateStamp YYYY:MM:DD
    im.save(p, format="JPEG", exif=ex)


def _catalogue(tmp_path):
    conn = connect(tmp_path / "c.sqlite3")
    return conn


def test_migration_creates_anchors_table(tmp_path):
    conn = _catalogue(tmp_path)
    assert conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] >= 7
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(anchors)")]
    assert {"scope", "scope_ref", "kind", "start_date", "end_date"} <= set(cols)


def test_anchor_add_validates(tmp_path):
    conn = _catalogue(tmp_path)
    with pytest.raises(ValueError):
        anchors.add_anchor(conn, "file", "f1", "range", "2004-01-01")           # range needs end
    with pytest.raises(ValueError):
        anchors.add_anchor(conn, "file", "f1", "range", "2004-02-01", "2004-01-01")  # end<start
    with pytest.raises(ValueError):
        anchors.add_anchor(conn, "bogus", "f1", "exact", "2004-01-01")          # bad scope


def test_anchor_resolution_prefers_most_specific(tmp_path):
    lib = tmp_path / "lib"
    _jpg(lib / "F.jpg", "2015:01:01 00:00:00")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib)
    lid = catalogue.list_libraries(conn)[0].id
    anchors.add_anchor(conn, "library", str(lid), "exact", "2000-01-01")
    anchors.add_anchor(conn, "directory", "trip", "exact", "2001-01-01", library_id=lid)
    anchors.add_anchor(conn, "file", "F", "exact", "2002-01-01", library_id=lid)
    a = anchors.list_anchors(conn)
    assert anchors.resolve_for(a, file_id="F", directory="trip", library_id=lid).start_date.year == 2002
    assert anchors.resolve_for(a, file_id="X", directory="trip", library_id=lid).start_date.year == 2001
    assert anchors.resolve_for(a, file_id="X", directory="other", library_id=lid).start_date.year == 2000


def test_gps_corroboration_makes_trusted(tmp_path):
    lib = tmp_path / "lib"
    _jpg(lib / "DSC_0001.jpg", "2015:06:01 12:00:00", "Nikon", "D3", gps="2015:06:01")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    _, res = analyse_library_reconciled(conn, now=NOW)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    assert res[fid].reliability is Reliability.TRUSTED


def test_gps_contradiction_condemns_reset_run(tmp_path):
    lib = tmp_path / "lib"
    for i in range(6):
        gps = "2004:12:25" if i == 3 else None
        _jpg(lib / "x" / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*3:02d}:00",
             "Canon", "5D", gps=gps, serial="SN-7")   # confirmed single device
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    _, res = analyse_library_reconciled(conn, now=NOW)
    ids = [r["id"] for r in conn.execute("SELECT id FROM files")]
    assert all(res[i].reliability is Reliability.LIKELY_WRONG for i in ids)


def test_manufacture_floor_condemns_impossible_date(tmp_path):
    lib = tmp_path / "lib"
    _jpg(lib / "IMG_0001.jpg", "2001:01:01 09:00:00", "Canon", "A70")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    floors = CameraFloors.from_dict({"canon|A70": "2003-01-01"})
    _, res = analyse_library_reconciled(conn, now=NOW, camera_floors=floors)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    assert res[fid].reliability is Reliability.LIKELY_WRONG
    # Unknown model -> no floor -> no conclusion.
    _, res2 = analyse_library_reconciled(conn, now=NOW)
    assert res2[fid].reliability is Reliability.QUESTIONABLE


def test_exact_file_anchor_beats_directory_and_trusts(tmp_path):
    lib = tmp_path / "lib"
    _jpg(lib / "a" / "IMG_0201.jpg", "2001:01:01 09:00:00", "Canon", "A70")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "directory", "a", "range", "2004-12-20", "2004-12-31")
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    _, res = analyse_library_reconciled(conn, now=NOW)
    assert res[fid].reliability is Reliability.TRUSTED
    assert res[fid].date == datetime(2004, 12, 25, tzinfo=timezone.utc)


def test_reconciliation_is_read_only(tmp_path):
    lib = tmp_path / "lib"
    _jpg(lib / "IMG_0001.jpg", "2015:06:01 12:00:00", "Nikon", "D3", gps="2015:06:01")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    rev = conn.execute("SELECT current_revision_id FROM files").fetchone()["current_revision_id"]
    analyse_library_reconciled(conn, now=NOW)
    after = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    rev2 = conn.execute("SELECT current_revision_id FROM files").fetchone()["current_revision_id"]
    assert after == before and rev == rev2


def test_gps_resolves_reset_epoch_end_to_end(tmp_path):
    # A single reset-epoch photo (QUESTIONABLE, reset_epoch doubt only) whose GPS
    # confirms the same date should become TRUSTED through the full pipeline.
    lib = tmp_path / "lib"
    _jpg(lib / "IMG_0001.jpg", "2001:01:01 09:00:00", "Nikon", "D3", gps="2001:01:01")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    _, res = analyse_library_reconciled(conn, now=NOW)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    assert res[fid].reliability is Reliability.TRUSTED


def test_reset_contradiction_does_not_propagate_across_model_only_camera_group(tmp_path):
    # Two serial-less Canon A70 bodies collapse to one camera_id; a reset run
    # spanning both must NOT be condemned wholesale from one GPS contradiction.
    lib = tmp_path / "lib"
    for i in range(6):
        gps = "2004:12:25" if i == 1 else None
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*3:02d}:00",
             "Canon", "A70", gps=gps)
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM cameras").fetchone()["n"] == 1
    _, res = analyse_library_reconciled(conn, now=NOW)
    condemned = sum(1 for fa in res.values() if fa.reliability is Reliability.LIKELY_WRONG)
    assert condemned == 1     # only the GPS-contradicted frame


def test_reset_contradiction_propagates_for_confirmed_device(tmp_path):
    lib = tmp_path / "lib"
    for i in range(6):
        gps = "2004:12:25" if i == 1 else None
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*3:02d}:00",
             "Canon", "5D", gps=gps, serial="SN-42")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    _, res = analyse_library_reconciled(conn, now=NOW)
    ids = [r["id"] for r in conn.execute("SELECT id FROM files")]
    assert all(res[i].reliability is Reliability.LIKELY_WRONG for i in ids)


def test_placeholder_serial_group_does_not_propagate(tmp_path):
    # Serial present but generic ('00000000') is not a credible unique device.
    lib = tmp_path / "lib"
    for i in range(6):
        gps = "2004:12:25" if i == 1 else None
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*3:02d}:00",
             "Canon", "A70", gps=gps, serial="00000000")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    _, res = analyse_library_reconciled(conn, now=NOW)
    condemned = sum(1 for fa in res.values() if fa.reliability is Reliability.LIKELY_WRONG)
    assert condemned == 1


def test_export_reconciliation_csv(tmp_path):
    from ppa.reconcile import export_reconciliation_csv
    lib = tmp_path / "lib"
    _jpg(lib / "good.jpg", "2015:06:01 12:00:00", "Nikon", "D3", gps="2015:06:01")
    _jpg(lib / "reset.jpg", "2001:01:01 09:00:00", "Canon", "A70")
    conn = _catalogue(tmp_path)
    scan_library(conn, lib); metadata.extract_stale(conn)
    out = tmp_path / "report.csv"
    n = export_reconciliation_csv(conn, out)
    assert n == 2 and out.exists()
    header, *rows = out.read_text().strip().splitlines()
    assert header.startswith("file_id,path,rating")
    assert len(rows) == 2
    # LIKELY_WRONG/QUESTIONABLE sort before TRUSTED, so the reset photo is first.
    assert "reset.jpg" in rows[0]
