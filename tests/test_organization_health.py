from pathlib import Path
import json

from ppa import catalogue
from ppa.db import connect
from ppa.organization import create_album, create_tag, add_photo_to_album, tag_photo
from ppa.organization_health import build_gap_browse, build_organization_health, ORGANIZATION_HEALTH_SCHEMA
from ppa.organization_views import save_organization_view


def _cfg(tmp_path: Path):
    from ppa.config import Config
    from ppa.scanner import scan_library
    lib = tmp_path / "library"; lib.mkdir()
    from PIL import Image
    for i in range(4):
        Image.new("RGB", (8, 8), (i * 20, 10, 10)).save(lib / f"p{i}.jpg")
    cfg = Config(tmp_path / "data" / "ppa.db", "INFO", tmp_path / "data" / "ppa.log", [lib])
    conn = connect(cfg.db_path)
    scan_library(conn, lib)
    conn.close()
    return cfg, lib


def test_health_separates_unorganized_no_album_and_no_tag(tmp_path: Path):
    cfg, _ = _cfg(tmp_path); conn = connect(cfg.db_path)
    lib = catalogue.list_libraries(conn)[0].id
    pids = tuple(dict.fromkeys(i.photo_id for i in catalogue.grid_items(conn)))
    album = create_album(conn, library_id=lib, name="A")
    tag = create_tag(conn, library_id=lib, name="T")
    add_photo_to_album(conn, album.id, pids[0]); tag_photo(conn, tag.id, pids[1])
    h = build_organization_health(conn, library_id=lib)
    assert h.schema == ORGANIZATION_HEALTH_SCHEMA and h.read_only
    assert set(h.unorganized_photo_ids) == set(pids[2:])
    assert set(h.no_album_photo_ids) == set(pids[1:])
    assert set(h.no_tag_photo_ids) == {pids[0], *pids[2:]}
    conn.close()


def test_health_detects_empty_album_unused_tag_and_missing_only_member(tmp_path: Path):
    cfg, libpath = _cfg(tmp_path); conn = connect(cfg.db_path)
    lib = catalogue.list_libraries(conn)[0].id; item = catalogue.grid_items(conn)[0]
    empty = create_album(conn, library_id=lib, name="Empty")
    unused = create_tag(conn, library_id=lib, name="Unused")
    album = create_album(conn, library_id=lib, name="Offline")
    tag = create_tag(conn, library_id=lib, name="OfflineTag")
    add_photo_to_album(conn, album.id, item.photo_id); tag_photo(conn, tag.id, item.photo_id)
    Path(item.path).unlink()
    conn.execute("UPDATE files SET presence_status='missing' WHERE photo_id=? AND library_id=?", (item.photo_id, lib)); conn.commit()
    h = build_organization_health(conn, library_id=lib)
    assert empty.id in h.empty_album_ids and unused.id in h.unused_tag_ids
    assert album.id in h.albums_with_missing_only_members
    assert tag.id in h.tags_with_missing_only_members
    conn.close()


def test_health_detects_broken_saved_view_without_mutating_it(tmp_path: Path):
    cfg, _ = _cfg(tmp_path); conn = connect(cfg.db_path)
    lib = catalogue.list_libraries(conn)[0].id
    album = create_album(conn, library_id=lib, name="A")
    view = save_organization_view(conn, library_id=lib, name="Saved", album_ids=[album.id])
    conn.execute("DELETE FROM albums WHERE id=?", (album.id,)); conn.commit()
    before = conn.total_changes
    h = build_organization_health(conn, library_id=lib)
    assert view.id in h.broken_saved_view_ids
    assert conn.total_changes == before
    conn.close()


def test_gap_browse_is_logical_photo_read_only(tmp_path: Path):
    cfg, _ = _cfg(tmp_path); conn = connect(cfg.db_path)
    lib = catalogue.list_libraries(conn)[0].id
    before = conn.total_changes; h = build_organization_health(conn, library_id=lib)
    view = build_gap_browse(conn, h, "unorganized")
    assert view.total_members == h.unorganized_count
    assert len({i.photo_id for i in view.items}) == view.total_members
    assert conn.total_changes == before
    conn.close()


def test_health_never_touches_evidence_tables(tmp_path: Path):
    cfg, _ = _cfg(tmp_path); conn = connect(cfg.db_path)
    lib = catalogue.list_libraries(conn)[0].id
    tables = ("metadata_observations", "anchors", "reconstructions")
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    h = build_organization_health(conn, library_id=lib)
    _ = h.to_json()
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    assert after == before
    conn.close()


def test_health_uses_bounded_selects_and_never_writes_source(tmp_path: Path):
    cfg, libpath = _cfg(tmp_path); conn = connect(cfg.db_path)
    lib = catalogue.list_libraries(conn)[0].id
    source = libpath / "p0.jpg"; before_bytes = source.read_bytes(); before_mtime = source.stat().st_mtime_ns
    selects = []
    conn.set_trace_callback(lambda sql: selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
    health = build_organization_health(conn, library_id=lib)
    conn.set_trace_callback(None)
    assert health.total_photos == 4
    assert len(selects) <= 8
    assert source.read_bytes() == before_bytes and source.stat().st_mtime_ns == before_mtime
    conn.close()
