from __future__ import annotations

import json
from pathlib import Path
from PIL import ExifTags, Image
import pytest

from ppa import anchors, metadata
from ppa.db import connect
from ppa.pilot_audit import (AUDIT_SCHEMA, build_pilot_audit, compare_pilot_audits,
                             snapshot_from_dict)
from ppa.reconstruct_catalogue import confirm_reconstruction, store_reconstructions
from ppa.scanner import scan_library


def _jpg(path: Path, dto: str, serial="SN-AUDIT"):
    path.parent.mkdir(parents=True, exist_ok=True)
    im=Image.new("RGB",(24,18),"green"); ex=im.getexif()
    ex[0x010F]="Canon"; ex[0x0110]="5D"
    sub=ex.get_ifd(ExifTags.IFD.Exif); sub[0x9003]=dto; sub[0xA431]=serial
    im.save(path,"JPEG",exif=ex)


def _lib(tmp_path: Path,n=5):
    lib=tmp_path/"lib"
    for i in range(n): _jpg(lib/f"IMG_{100+i:04d}.jpg",f"2001:01:01 00:{i:02d}:00")
    conn=connect(tmp_path/"ppa.sqlite3"); scan_library(conn,lib); metadata.extract_stale(conn)
    lid=conn.execute("SELECT id FROM libraries").fetchone()[0]
    return conn,lib,lid


def test_audit_snapshot_is_read_only_and_traceable(tmp_path):
    conn,_,lid=_lib(tmp_path,5); before=conn.total_changes
    snap=build_pilot_audit(conn,library_id=lid,generated_at="fixed")
    assert snap.schema==AUDIT_SCHEMA and snap.read_only and snap.audit_source_writes==0
    assert snap.total_files==5 and conn.total_changes==before
    assert snap.unresolved.count==len(snap.unresolved.file_ids)


def test_audit_does_not_invent_before_state(tmp_path):
    conn,_,lid=_lib(tmp_path,2)
    snap=build_pilot_audit(conn,library_id=lid,generated_at="fixed")
    data=snap.to_dict()
    assert "before" not in data and "improved" not in data


def test_snapshot_round_trip_is_deterministic(tmp_path):
    conn,_,lid=_lib(tmp_path,3)
    snap=build_pilot_audit(conn,library_id=lid,generated_at="fixed")
    restored=snapshot_from_dict(json.loads(snap.to_json(pretty=False)))
    assert restored==snap


def test_compare_requires_same_scope(tmp_path):
    conn,lib,lid=_lib(tmp_path,3)
    a=build_pilot_audit(conn,library_id=lid,generated_at="A")
    b=build_pilot_audit(conn,library_id=lid,file_ids=[a.unresolved.file_ids[0]],generated_at="B")
    with pytest.raises(ValueError): compare_pilot_audits(a,b)


def test_compare_measures_real_human_progress(tmp_path):
    conn,_,lid=_lib(tmp_path,5)
    before=build_pilot_audit(conn,library_id=lid,generated_at="before")
    fid=conn.execute("SELECT id FROM files ORDER BY filename LIMIT 1").fetchone()[0]
    anchors.add_anchor(conn,"file",fid,"exact","2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn,fid)
    after=build_pilot_audit(conn,library_id=lid,generated_at="after")
    delta=compare_pilot_audits(before,after)
    assert delta.confirmed_current.delta==1
    assert delta.usable_chronology.delta>=1
    assert delta.unresolved.delta<=-1


def test_changed_evidence_surfaces_stale_decision(tmp_path):
    conn,_,lid=_lib(tmp_path,5)
    fid=conn.execute("SELECT id FROM files ORDER BY filename LIMIT 1").fetchone()[0]
    anchors.add_anchor(conn,"file",fid,"exact","2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn,fid)
    before=build_pilot_audit(conn,library_id=lid,generated_at="before")
    anchors.add_anchor(conn,"file",fid,"exact","2005-12-25")
    after=build_pilot_audit(conn,library_id=lid,generated_at="after")
    delta=compare_pilot_audits(before,after)
    assert delta.stale_decisions.delta>=1
    assert fid in after.stale_decisions.file_ids

def test_audit_reuses_one_expensive_pilot_analysis(tmp_path, monkeypatch):
    conn,_,lid=_lib(tmp_path,3)
    import ppa.pilot_audit as pa
    real=pa.analyse_pilot
    calls=[]
    def wrapped(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(pa, "analyse_pilot", wrapped)
    build_pilot_audit(conn, library_id=lid, generated_at="fixed")
    assert len(calls)==1
