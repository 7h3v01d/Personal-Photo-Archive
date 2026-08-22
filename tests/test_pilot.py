from __future__ import annotations

import json
from pathlib import Path
from PIL import ExifTags, Image
import pytest

from ppa import anchors, metadata
from ppa.db import connect
from ppa.pilot import REPORT_SCHEMA, analyse_pilot
from ppa.reconstruct_catalogue import confirm_reconstruction, store_reconstructions
from ppa.scanner import scan_library


def _jpg(path: Path, dto: str, *, serial="SN-PILOT", gps=None, color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (32, 24), color)
    ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
    sub = ex.get_ifd(ExifTags.IFD.Exif); sub[0x9003] = dto
    if serial is not None: sub[0xA431] = serial
    if gps:
        gps_ifd = ex.get_ifd(ExifTags.IFD.GPSInfo); gps_ifd[0x001D] = gps
    im.save(path, format="JPEG", exif=ex)


def _library(tmp_path: Path, name="lib", n=5, subdir="old"):
    lib = tmp_path/name
    for i in range(n):
        _jpg(lib/subdir/f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*5:02d}:00")
    conn=connect(tmp_path/f"{name}.sqlite3")
    scan_library(conn,lib); metadata.extract_stale(conn)
    lid=conn.execute("SELECT id FROM libraries").fetchone()["id"]
    return conn,lib,lid


def test_pilot_report_is_read_only_and_traceable(tmp_path):
    conn,lib,lid=_library(tmp_path)
    before_changes=conn.total_changes
    before_counts={t:conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                   for t in ("files","metadata_observations","anchors","reconstructions")}
    report=analyse_pilot(conn,library_id=lid,generated_at="fixed")
    after_counts={t:conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                  for t in before_counts}
    assert report.read_only and report.total_files==5
    assert after_counts==before_counts and conn.total_changes==before_changes
    assert sum(b.count for b in report.reliability.values())==5
    assert all(b.count==len(b.file_ids) for b in report.reliability.values())


def test_directory_scope_contains_only_that_directory(tmp_path):
    conn,lib,lid=_library(tmp_path,n=3,subdir="old")
    _jpg(lib/"new"/"IMG_0900.jpg","2019:01:01 10:00:00")
    scan_library(conn,lib); metadata.extract_stale(conn)
    report=analyse_pilot(conn,library_id=lid,directory_prefix="old",generated_at="fixed")
    assert report.total_files==3
    scoped={fid for b in report.reliability.values() for fid in b.file_ids}
    names={conn.execute("SELECT filename FROM files WHERE id=?",(fid,)).fetchone()[0] for fid in scoped}
    assert names=={"IMG_0201.jpg","IMG_0202.jpg","IMG_0203.jpg"}


def test_explicit_scope_rejects_cross_library_file(tmp_path):
    c1,_,l1=_library(tmp_path,"a",1)
    # second library must live in same catalogue for the cross-library check
    lib2=tmp_path/"b"; _jpg(lib2/"x.jpg","2019:01:01 00:00:00")
    scan_library(c1,lib2); metadata.extract_stale(c1)
    fid2=c1.execute("SELECT id FROM files WHERE library_id != ?",(l1,)).fetchone()[0]
    with pytest.raises(ValueError): analyse_pilot(c1,library_id=l1,file_ids=[fid2])


def test_empty_directory_scope_is_valid_zero_report(tmp_path):
    conn,_,lid=_library(tmp_path,n=1)
    r=analyse_pilot(conn,library_id=lid,directory_prefix="does-not-exist",generated_at="fixed")
    assert r.total_files==0 and r.total_photos==0
    assert sum(b.count for b in r.reliability.values())==0


def test_reset_pattern_creates_traceable_opportunity(tmp_path):
    conn,_,lid=_library(tmp_path,n=12)
    r=analyse_pilot(conn,library_id=lid,generated_at="fixed")
    assert len(r.reset_groups)==1
    assert r.reset_groups[0].identity_strength=="DEVICE_STRONG"
    assert r.anchor_opportunities and r.anchor_opportunities[0].affected_count>=10
    assert r.anchor_opportunities[0].priority=="A"


def test_confirmed_and_stale_are_separate(tmp_path):
    conn,_,lid=_library(tmp_path,n=5)
    fid=conn.execute("SELECT id FROM files ORDER BY filename LIMIT 1").fetchone()[0]
    anchors.add_anchor(conn,"file",fid,"exact","2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn,fid)
    fresh=analyse_pilot(conn,library_id=lid,generated_at="fixed")
    assert fresh.reconstruction["confirmed_current"].file_ids==(fid,)
    # change evidence, not bytes
    anchors.add_anchor(conn,"file",fid,"exact","2005-12-25")
    stale=analyse_pilot(conn,library_id=lid,generated_at="fixed")
    assert stale.reconstruction["confirmed_stale"].file_ids==(fid,)
    assert fid in stale.review_priority["A"].file_ids


def test_json_is_stable_except_generated_at(tmp_path):
    conn,_,lid=_library(tmp_path,n=2)
    a=analyse_pilot(conn,library_id=lid,generated_at="A").to_dict()
    b=analyse_pilot(conn,library_id=lid,generated_at="B").to_dict()
    a["generated_at"]=b["generated_at"]="X"
    assert a==b
    text=analyse_pilot(conn,library_id=lid,generated_at="fixed").to_json(pretty=False)
    parsed=json.loads(text)
    assert parsed["schema"]==REPORT_SCHEMA and parsed["read_only"] is True


def test_unknown_library_fails_closed(tmp_path):
    conn=connect(tmp_path/"c.sqlite3")
    with pytest.raises(ValueError): analyse_pilot(conn,library_id=999)


def test_pilot_progress_hooks_report_stages(tmp_path):
    conn,_,lid=_library(tmp_path,n=2)
    seen=[]
    analyse_pilot(conn,library_id=lid,generated_at="fixed",progress_cb=seen.append)
    assert seen[0].startswith("Date Review:")
    assert any("chronology" in s.lower() for s in seen)
    assert any("reconstruction freshness" in s.lower() for s in seen)
    assert any("metadata quality" in s.lower() for s in seen)


def test_pilot_cancellation_fails_without_writes(tmp_path):
    from ppa.pilot import PilotAnalysisCancelled
    conn,_,lid=_library(tmp_path,n=2)
    before=conn.total_changes
    with pytest.raises(PilotAnalysisCancelled):
        analyse_pilot(conn,library_id=lid,generated_at="fixed",cancel_cb=lambda: True)
    assert conn.total_changes==before
