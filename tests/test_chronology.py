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


def _photo(fid, name, dto, camera_id="cam-A"):
    intr = assess([DateObservation("exif", "DateTimeOriginal", dto)], now=NOW)
    seq, _ = filename_sequence(name)
    return SequencedPhoto(fid, name, seq, intr, camera_id)


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


def _exif_jpg(p: Path, dto: str, make: str, model: str, color="red"):
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (32, 24), color)
    ex = im.getexif()
    ex[0x010F] = make; ex[0x0110] = model
    ex.get_ifd(ExifTags.IFD.Exif)[0x9003] = dto
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


def test_order_conflict_doubts_both_photos_for_a_known_camera():
    reg = [_photo("r0", "IMG_0001.jpg", "2015:06:05 12:00:00", camera_id="cam-X"),
           _photo("r1", "IMG_0002.jpg", "2015:06:02 12:00:00", camera_id="cam-X")]
    findings, chron = analyse_sequence(reg)
    assert any(f.kind == "timestamp_order_conflict" for f in findings)
    # BOTH implicated claims inherit doubt, not just one.
    assert chron["r0"].reliability is Reliability.QUESTIONABLE
    assert chron["r1"].reliability is Reliability.QUESTIONABLE


def test_order_conflict_without_known_camera_is_reported_not_scored():
    reg = [_photo("r0", "IMG_0001.jpg", "2015:06:05 12:00:00", camera_id=None),
           _photo("r1", "IMG_0002.jpg", "2015:06:02 12:00:00", camera_id=None)]
    findings, chron = analyse_sequence(reg)
    assert any(f.kind == "timestamp_order_conflict" for f in findings)  # reported
    # ...but not scored down (could be interleaved cameras).
    assert chron["r0"].reliability is Reliability.PROBABLY_VALID
    assert chron["r1"].reliability is Reliability.PROBABLY_VALID


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
