from pathlib import Path
from PIL import ExifTags, Image

from ppa import anchors, metadata
from ppa.db import connect
from ppa.reconstruct_catalogue import confirm_reconstruction, store_reconstructions
from ppa.scanner import scan_library
from ppa.unresolved import UNRESOLVED_SCHEMA, build_unresolved_memories


def _jpg(path: Path, dto: str | None, serial="SN-UNRES"):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (24, 18), "blue")
    ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
    sub = ex.get_ifd(ExifTags.IFD.Exif)
    if dto is not None: sub[0x9003] = dto
    if serial is not None: sub[0xA431] = serial
    im.save(path, "JPEG", exif=ex)


def _lib(tmp_path: Path, n=5):
    lib = tmp_path / "lib"
    for i in range(n):
        _jpg(lib / f"IMG_{100+i:04d}.jpg", f"2001:01:01 00:{i:02d}:00")
    conn = connect(tmp_path / "ppa.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    return conn, lib, lid


def test_unresolved_view_is_read_only_partition(tmp_path):
    conn, _, lid = _lib(tmp_path, 5)
    before = conn.total_changes
    view = build_unresolved_memories(conn, library_id=lid)
    assert view.schema == UNRESOLVED_SCHEMA and view.read_only
    assert view.unresolved_count == 5
    assert sum(c.count for c in view.categories) == view.unresolved_count
    assert conn.total_changes == before


def test_strong_reset_run_without_anchor_is_explicit(tmp_path):
    conn, _, lid = _lib(tmp_path, 5)
    view = build_unresolved_memories(conn, library_id=lid)
    assert {i.category for i in view.items} == {"RESET_RUN_WITHOUT_EXACT_ANCHOR"}
    assert all(i.reset_group_size == 5 for i in view.items)


def test_range_anchor_is_preserved_as_range_only_knowledge(tmp_path):
    conn, _, lid = _lib(tmp_path, 2)
    # Break sequence grouping by removing strong identity from one file's camera via a separate file scope.
    fid = conn.execute("SELECT id FROM files ORDER BY filename LIMIT 1").fetchone()[0]
    anchors.add_anchor(conn, "file", fid, "range", "2001-01-01", "2001-01-31")
    view = build_unresolved_memories(conn, library_id=lid, file_ids=[fid])
    assert view.items[0].category == "RANGE_ONLY_KNOWLEDGE"
    assert view.items[0].has_range_anchor


def test_fresh_confirmed_is_not_unresolved_but_stale_confirmed_is(tmp_path):
    conn, _, lid = _lib(tmp_path, 5)
    fid = conn.execute("SELECT id FROM files ORDER BY filename LIMIT 1").fetchone()[0]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    fresh = build_unresolved_memories(conn, library_id=lid)
    assert fid not in {i.file_id for i in fresh.items}
    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")
    stale = build_unresolved_memories(conn, library_id=lid)
    item = next(i for i in stale.items if i.file_id == fid)
    assert item.category == "STALE_DECISION_NEEDS_REVIEW" and item.stale


def test_missing_date_becomes_no_usable_evidence(tmp_path):
    lib = tmp_path / "lib"; _jpg(lib / "orphan.jpg", None, serial=None)
    conn = connect(tmp_path / "ppa.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    view = build_unresolved_memories(conn, library_id=lid)
    assert view.items[0].category == "NO_USABLE_DATE_EVIDENCE"


def test_unresolved_json_deterministic(tmp_path):
    conn, _, lid = _lib(tmp_path, 3)
    a = build_unresolved_memories(conn, library_id=lid).to_json(pretty=False)
    b = build_unresolved_memories(conn, library_id=lid).to_json(pretty=False)
    assert a == b


def test_clean_probably_valid_recorded_date_is_not_unresolved(tmp_path):
    lib = tmp_path / "lib"
    _jpg(lib / "normal.jpg", "2019:06:15 12:30:00", serial="SN-NORMAL")
    conn = connect(tmp_path / "ppa.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    view = build_unresolved_memories(conn, library_id=lid)
    # Healthy recorded chronology does not require Phase-7 confirmation merely
    # because there is no reconstruction row.
    assert view.unresolved_count == 0
