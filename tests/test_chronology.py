"""Phase 6 Slice 2 — cross-photo / sequence chronology engine.

Corrected semantics: filename order establishes SEQUENCE, not calendar truth.
A reset pattern is flagged but never escalated to LIKELY_WRONG on order evidence
alone; order conflicts doubt BOTH implicated photos (for a confirmed camera);
Slice 2 only ever adds doubt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PIL import ExifTags, Image

from ppa import metadata
from ppa.chronology import (
    SequencedPhoto,
    analyse_library,
    analyse_sequence,
    filename_sequence,
)
from ppa.dating import DateObservation, Reliability, assess
from ppa.db import connect
from ppa.scanner import scan_library

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _photo(fid, name, dto, camera_id="cam-A", strong_device_identity=True):
    intr = assess([DateObservation("exif", "DateTimeOriginal", dto)], now=NOW)
    seq, _ = filename_sequence(name)
    return SequencedPhoto(fid, name, seq, intr, camera_id, strong_device_identity)


def test_filename_sequence_parsing():
    assert filename_sequence("IMG_0201.jpg") == (201, "IMG_")
    assert filename_sequence("DSC00042.JPG") == (42, "DSC")
    assert filename_sequence("holiday.png") == (None, "holiday")


# --- the P0: order evidence must not decide calendar truth -------------------


def test_genuine_jan1_photos_are_not_escalated():
    # Five real photos taken on 1 Jan 2001 look identical to a reset run.
    g = [_photo(f"g{i}", f"IMG_{i+1:04d}.jpg", f"2001:01:01 12:0{i+1}:00") for i in range(5)]
    findings, chron = analyse_sequence(g)
    assert any(f.kind == "reset_pattern" for f in findings)   # pattern IS flagged
    for i in range(5):
        assert chron[f"g{i}"].reliability is Reliability.QUESTIONABLE  # NOT LIKELY_WRONG


def test_reset_pattern_is_emitted_but_reliability_unchanged():
    run = [_photo(f"f{i}", f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*3:02d}:00") for i in range(8)]
    findings, chron = analyse_sequence(run)
    assert any(f.kind == "reset_pattern" for f in findings)
    for i in range(8):
        assert chron[f"f{i}"].intrinsic is Reliability.QUESTIONABLE
        assert chron[f"f{i}"].reliability is Reliability.QUESTIONABLE  # unchanged
        assert chron[f"f{i}"].cross_photo_reasons


def test_slice2_never_upgrades_reliability():
    solo = [_photo("x0", "IMG_0001.jpg", "2001:01:01 09:00:00")]
    _, chron = analyse_sequence(solo)
    assert chron["x0"].reliability is Reliability.QUESTIONABLE


# --- segmentation ------------------------------------------------------------


def test_non_consecutive_files_do_not_form_a_reset_pattern():
    nc = [_photo("n0", "IMG_0001.jpg", "2001:01:01 00:01:00"),
          _photo("n1", "IMG_0100.jpg", "2001:01:01 00:05:00"),
          _photo("n2", "IMG_5000.jpg", "2001:01:01 00:11:00"),
          _photo("n3", "IMG_8000.jpg", "2001:01:01 01:00:00"),
          _photo("n4", "IMG_9999.jpg", "2001:01:01 03:00:00")]
    findings, chron = analyse_sequence(nc)
    assert not any(f.kind == "reset_pattern" for f in findings)   # gaps break segments
    for i in range(5):
        assert chron[f"n{i}"].reliability is Reliability.QUESTIONABLE  # intrinsic only


def test_large_filename_gap_breaks_segment():
    seq = [_photo("a", "IMG_0201.jpg", "2001:01:01 00:01:00"),
           _photo("b", "IMG_0202.jpg", "2001:01:01 00:02:00"),
           _photo("c", "IMG_0203.jpg", "2001:01:01 00:03:00"),
           _photo("d", "IMG_6531.jpg", "2001:01:01 00:04:00"),  # huge jump
           _photo("e", "IMG_6532.jpg", "2001:01:01 00:05:00")]
    findings, _ = analyse_sequence(seq, min_reset_run=3)
    # Two segments of 3 and 2; only the first reaches the run threshold.
    resets = [f for f in findings if f.kind == "reset_pattern"]
    assert len(resets) == 1 and len(resets[0].file_ids) == 3


def test_same_camera_contiguous_reset_pattern_still_detected():
    run = [_photo(f"f{i}", f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*3:02d}:00") for i in range(6)]
    findings, _ = analyse_sequence(run)
    assert any(f.kind == "reset_pattern" for f in findings)


# --- camera grouping ---------------------------------------------------------


def _exif_jpg(p: Path, dto: str, make: str, model: str, color="red", serial=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (32, 24), color)
    ex = im.getexif()
    ex[0x010F] = make; ex[0x0110] = model
    sub = ex.get_ifd(ExifTags.IFD.Exif)
    sub[0x9003] = dto
    if serial is not None:
        sub[0xA431] = serial                 # BodySerialNumber
    im.save(p, format="JPEG", exif=ex)


def test_two_cameras_sharing_img_prefix_do_not_share_a_sequence(tmp_path):
    lib = tmp_path / "lib"
    # Two cameras, both naming files IMG_*, interleaved in one folder.
    for i in range(6):
        _exif_jpg(lib / f"IMG_{200+2*i:04d}.jpg", f"2001:01:01 00:{i*3:02d}:00",
                  "Canon", "A70", color="red")
        _exif_jpg(lib / f"IMG_{201+2*i:04d}.jpg", "2018:07:07 07:07:07",
                  "Nikon", "D3", color="blue")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)

    findings, chron = analyse_library(conn, now=NOW)
    # The Nikon photos (real 2018) must not be dragged into the Canon reset run.
    nikon = [r["id"] for r in conn.execute(
        "SELECT f.id FROM files f JOIN cameras c ON c.id=f.camera_id WHERE c.make='Nikon'")]
    assert nikon and all(chron[i].reliability is Reliability.PROBABLY_VALID for i in nikon)


def test_same_model_without_serial_does_not_score_order_conflicts(tmp_path):
    # Two DIFFERENT serial-less Canon A70 bodies collapse to one camera record.
    # Interleaved in one folder, their (legitimate) dates conflict in filename
    # order — but that must NOT be scored down: it may just be two cameras.
    lib = tmp_path / "lib"
    _exif_jpg(lib / "IMG_0001.jpg", "2018:07:01 10:00:00", "Canon", "A70", color="red")
    _exif_jpg(lib / "IMG_0002.jpg", "2010:07:01 10:00:00", "Canon", "A70", color="blue")
    _exif_jpg(lib / "IMG_0003.jpg", "2018:07:01 11:00:00", "Canon", "A70", color="green")
    _exif_jpg(lib / "IMG_0004.jpg", "2010:07:01 11:00:00", "Canon", "A70", color="orange")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM cameras").fetchone()["n"] == 1  # collapsed

    findings, chron = analyse_library(conn, now=NOW)
    assert any(f.kind == "timestamp_order_conflict" for f in findings)   # still reported
    ids = [r["id"] for r in conn.execute("SELECT id FROM files")]
    assert all(chron[i].reliability is Reliability.PROBABLY_VALID for i in ids)  # not scored


def test_confirmed_device_order_conflict_is_scored(tmp_path):
    # One physical body (serial present); a real backwards timestamp is scored.
    lib = tmp_path / "lib"
    _exif_jpg(lib / "IMG_0001.jpg", "2018:07:05 10:00:00", "Canon", "5D", serial="SN-123")
    _exif_jpg(lib / "IMG_0002.jpg", "2018:07:02 10:00:00", "Canon", "5D", serial="SN-123")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)

    findings, chron = analyse_library(conn, now=NOW)
    assert any(f.kind == "timestamp_order_conflict" for f in findings)
    ids = [r["id"] for r in conn.execute("SELECT id FROM files")]
    assert all(chron[i].reliability is Reliability.QUESTIONABLE for i in ids)  # both doubted


def test_placeholder_serial_is_not_strong_identity(tmp_path):
    # Two different A70 bodies both emit a generic serial 00000000; they collapse
    # to one camera record. A placeholder serial must NOT license scoring.
    lib = tmp_path / "lib"
    _exif_jpg(lib / "IMG_0001.jpg", "2018:07:01 10:00:00", "Canon", "A70", serial="00000000")
    _exif_jpg(lib / "IMG_0002.jpg", "2010:07:01 10:00:00", "Canon", "A70", serial="00000000")
    _exif_jpg(lib / "IMG_0003.jpg", "2018:07:01 11:00:00", "Canon", "A70", serial="00000000")
    _exif_jpg(lib / "IMG_0004.jpg", "2010:07:01 11:00:00", "Canon", "A70", serial="00000000")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM cameras").fetchone()["n"] == 1

    findings, chron = analyse_library(conn, now=NOW)
    assert any(f.kind == "timestamp_order_conflict" for f in findings)   # still reported
    ids = [r["id"] for r in conn.execute("SELECT id FROM files")]
    assert all(chron[i].reliability is Reliability.PROBABLY_VALID for i in ids)  # not scored


def test_is_strong_serial_rejects_placeholders_and_repeats():
    from ppa.chronology import _is_strong_serial
    for good in ("SN-123456", "SN12345678", "1234"):
        assert _is_strong_serial(good)
    for bad in (None, "", "0", "00000000", "000000000000", "UNKNOWN", "unknown",
                "N/A", "na", "NOT AVAILABLE", "-", "111111111", "FFFFFFFF", "  0000  "):
        assert not _is_strong_serial(bad)


def test_analyse_sequence_sorts_defensively():
    # Given out-of-order input, the engine still segments by true sequence order.
    photos = [_photo("p3", "IMG_0203.jpg", "2001:01:01 00:07:00"),
              _photo("p1", "IMG_0201.jpg", "2001:01:01 00:01:00"),
              _photo("p2", "IMG_0202.jpg", "2001:01:01 00:04:00"),
              _photo("p4", "IMG_0204.jpg", "2001:01:01 00:11:00"),
              _photo("p5", "IMG_0205.jpg", "2001:01:01 00:14:00")]
    findings, _ = analyse_sequence(photos, min_reset_run=5)
    resets = [f for f in findings if f.kind == "reset_pattern"]
    assert len(resets) == 1 and len(resets[0].file_ids) == 5




def test_order_conflict_doubts_both_photos_for_a_confirmed_device():
    reg = [_photo("r0", "IMG_0001.jpg", "2015:06:05 12:00:00", camera_id="cam-X", strong_device_identity=True),
           _photo("r1", "IMG_0002.jpg", "2015:06:02 12:00:00", camera_id="cam-X", strong_device_identity=True)]
    findings, chron = analyse_sequence(reg)
    assert any(f.kind == "timestamp_order_conflict" for f in findings)
    # BOTH implicated claims inherit doubt, not just one.
    assert chron["r0"].reliability is Reliability.QUESTIONABLE
    assert chron["r1"].reliability is Reliability.QUESTIONABLE


def test_order_conflict_without_confirmed_device_is_reported_not_scored():
    # Same camera cluster but no serial (model-only) — may be two bodies.
    reg = [_photo("r0", "IMG_0001.jpg", "2015:06:05 12:00:00", camera_id="cam-A", strong_device_identity=False),
           _photo("r1", "IMG_0002.jpg", "2015:06:02 12:00:00", camera_id="cam-A", strong_device_identity=False)]
    findings, chron = analyse_sequence(reg)
    assert any(f.kind == "timestamp_order_conflict" for f in findings)  # reported
    assert chron["r0"].reliability is Reliability.PROBABLY_VALID        # not scored
    assert chron["r1"].reliability is Reliability.PROBABLY_VALID


def test_duplicate_sequence_numbers_are_ambiguous_order():
    from ppa.chronology import _segment
    photos = [_photo("d0", "IMG_0001.jpg", "2015:06:01 12:00:00"),
              _photo("d1", "IMG_0001.raw", "2015:06:02 12:00:00"),
              _photo("d2", "IMG_0002.jpg", "2015:06:03 12:00:00")]
    photos.sort(key=lambda p: (p.seq, p.filename))
    sizes = [len(s) for s in _segment(photos, max_seq_gap=10)]
    assert sizes[0] == 1   # the duplicate-seq pair is split, not treated as adjacency


def test_analyse_library_is_read_only(tmp_path):
    lib = tmp_path / "lib"
    for i in range(6):
        _exif_jpg(lib / "s" / f"IMG_{201+i:04d}.jpg", f"2000:01:01 00:{i*4:02d}:00",
                  "Canon", "A70")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    findings, chron = analyse_library(conn, now=NOW)
    assert any(f.kind == "reset_pattern" for f in findings)
    assert all(c.reliability is Reliability.QUESTIONABLE for c in chron.values())  # not escalated
    after = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    assert after == before
