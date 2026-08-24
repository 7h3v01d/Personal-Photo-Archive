from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ppa import catalogue
from ppa.db import connect
from ppa.organization import add_photo_to_album, create_album, create_tag, tag_photo
from ppa.organization_views import (
    delete_organization_view,
    evaluate_organization_view,
    get_organization_view,
    list_organization_views,
    save_organization_view,
)


def _library(tmp_path: Path, name: str = "library"):
    from PIL import Image
    from ppa.config import Config
    from ppa.scanner import scan_library
    root = tmp_path / name; root.mkdir()
    for idx in range(3):
        Image.new("RGB", (8, 8), (idx * 40, 10, 20)).save(root / f"p{idx}.jpg")
    cfg = Config(db_path=tmp_path / f"{name}.sqlite3", log_level="INFO", log_path=tmp_path / "ppa.log", library_directories=[])
    conn = connect(cfg.db_path); scan_library(conn, root)
    lib_id = catalogue.list_libraries(conn)[0].id
    pids = tuple(dict.fromkeys(i.photo_id for i in catalogue.grid_items(conn)))
    return conn, lib_id, pids


def test_schema_v21_and_saved_organization_view_roundtrip(tmp_path: Path):
    conn, lib_id, pids = _library(tmp_path)
    album=create_album(conn,library_id=lib_id,name="Holiday"); tag=create_tag(conn,library_id=lib_id,name="Family")
    v=save_organization_view(conn,library_id=lib_id,name=" Holiday family ",album_ids=[album.id,album.id],tag_ids=[tag.id])
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 21
    assert v.name == "Holiday family" and v.album_ids == (album.id,) and v.tag_ids == (tag.id,)
    assert get_organization_view(conn,v.id) == v
    assert list_organization_views(conn,library_id=lib_id) == (v,)
    conn.close()


def test_saved_view_is_recipe_not_cached_results(tmp_path: Path):
    conn, lib_id, pids = _library(tmp_path)
    album=create_album(conn,library_id=lib_id,name="Holiday"); tag=create_tag(conn,library_id=lib_id,name="Family")
    add_photo_to_album(conn,album.id,pids[0]); tag_photo(conn,tag.id,pids[0])
    v=save_organization_view(conn,library_id=lib_id,name="Holiday family",album_ids=[album.id],tag_ids=[tag.id])
    assert evaluate_organization_view(conn,v).view.total_members == 1
    add_photo_to_album(conn,album.id,pids[1]); tag_photo(conn,tag.id,pids[1])
    assert evaluate_organization_view(conn,v).view.total_members == 2
    cols={r[1] for r in conn.execute("PRAGMA table_info(saved_organization_views)")}
    assert "photo_ids" not in cols and "photo_ids_json" not in cols
    conn.close()


def test_same_name_updates_recipe_and_delete(tmp_path: Path):
    conn, lib_id, pids = _library(tmp_path)
    a=create_album(conn,library_id=lib_id,name="A"); b=create_album(conn,library_id=lib_id,name="B")
    v1=save_organization_view(conn,library_id=lib_id,name="Keep",album_ids=[a.id])
    v2=save_organization_view(conn,library_id=lib_id,name="keep",album_ids=[b.id])
    assert v2.id == v1.id and v2.album_ids == (b.id,)
    assert delete_organization_view(conn,v1.id)
    assert not delete_organization_view(conn,v1.id)
    conn.close()


def test_saved_view_cross_library_and_unknown_selector_fail_closed(tmp_path: Path):
    # One DB, two library roots.
    from PIL import Image
    from ppa.config import Config
    from ppa.scanner import scan_library
    cfg=Config(db_path=tmp_path/"db.sqlite3", log_level="INFO", log_path=tmp_path/"ppa.log", library_directories=[]); conn=connect(cfg.db_path)
    r1=tmp_path/"l1"; r2=tmp_path/"l2"; r1.mkdir(); r2.mkdir(); Image.new("RGB",(8,8)).save(r1/"a.jpg"); Image.new("RGB",(8,8)).save(r2/"b.jpg")
    scan_library(conn,r1); scan_library(conn,r2); libs=catalogue.list_libraries(conn); l1,l2=libs[0].id,libs[1].id
    a=create_album(conn,library_id=l1,name="A"); t=create_tag(conn,library_id=l2,name="T")
    with pytest.raises(ValueError): save_organization_view(conn,library_id=l1,name="bad",album_ids=[a.id],tag_ids=[t.id])
    with pytest.raises(ValueError): save_organization_view(conn,library_id=l1,name="bad2",album_ids=["missing"])
    conn.close()


def test_saved_view_evaluation_never_changes_evidence(tmp_path: Path):
    conn, lib_id, pids = _library(tmp_path)
    a=create_album(conn,library_id=lib_id,name="Christmas 2004"); t=create_tag(conn,library_id=lib_id,name="25 December 2004")
    add_photo_to_album(conn,a.id,pids[0]); tag_photo(conn,t.id,pids[0])
    v=save_organization_view(conn,library_id=lib_id,name="Date-looking recipe",album_ids=[a.id],tag_ids=[t.id])
    tables=("metadata_observations","anchors","reconstructions","events")
    before={x:conn.execute(f"SELECT COUNT(*) FROM {x}").fetchone()[0] for x in tables}; changes=conn.total_changes
    result=evaluate_organization_view(conn,v)
    after={x:conn.execute(f"SELECT COUNT(*) FROM {x}").fetchone()[0] for x in tables}
    assert result.read_only and result.view.total_members == 1 and before == after and conn.total_changes == changes
    conn.close()
