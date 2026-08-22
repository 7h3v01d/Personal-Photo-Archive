from pathlib import Path

from PIL import ExifTags, Image

from ppa import anchors, metadata
from ppa.db import connect
from ppa.evidence_inspector import inspect_date_evidence, concise_text
from ppa.reconstruct_catalogue import store_reconstructions
from ppa.scanner import scan_library


def _jpg(p: Path, dto: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (32, 24), "red")
    ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "A70"
    sub = ex.get_ifd(ExifTags.IFD.Exif); sub[0x9003] = dto; sub[0xA431] = "ABC123"
    im.save(p, format="JPEG", exif=ex)


def _setup(tmp_path):
    lib = tmp_path / "lib"
    _jpg(lib / "IMG_0001.jpg", "2001:01:01 00:00:00")
    c = connect(tmp_path / "x.db")
    scan_library(c, lib); metadata.extract_stale(c)
    fid = c.execute("SELECT id FROM files WHERE filename='IMG_0001.jpg'").fetchone()[0]
    return c, fid


def test_trace_is_read_only_and_explains_direct_anchor(tmp_path):
    c, fid = _setup(tmp_path)
    anchors.add_anchor(c, "file", fid, "exact", "2004-12-25")
    store_reconstructions(c)
    before = c.total_changes
    t = inspect_date_evidence(c, fid)
    assert c.total_changes == before
    assert t.independent_evidence == ("Exact human date: 2004-12-25",)
    assert t.reconstruction_method == "direct"
    assert any("exact human/local" in x.lower() for x in t.derivation)
    assert "Read-only explanation" in concise_text(t)


def test_trace_rejects_unknown_file(tmp_path):
    c, _ = _setup(tmp_path)
    try:
        inspect_date_evidence(c, "missing")
    except ValueError as exc:
        assert "present file not found" in str(exc)
    else:
        raise AssertionError("expected fail-closed unknown file")


def test_trace_explains_offset_basis_and_group(tmp_path):
    lib = tmp_path / "run"
    for i in range(5):
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*5:02d}:00")
    c = connect(tmp_path / "offset.db")
    scan_library(c, lib); metadata.extract_stale(c)
    ids = {r["filename"]: r["id"] for r in c.execute("SELECT id, filename FROM files")}
    anchors.add_anchor(c, "file", ids["IMG_0203.jpg"], "exact", "2004-12-25")
    store_reconstructions(c)
    t = inspect_date_evidence(c, ids["IMG_0201.jpg"])
    assert t.reconstruction_method == "offset"
    assert t.reset_group_strong is True
    assert len(t.reset_group_members) == 5
    assert any("day camera-clock offset" in x for x in t.derivation)
    assert any("IMG_0203.jpg" in x for x in t.derivation)
    assert any("credible single-device identity" in x for x in t.derivation)
