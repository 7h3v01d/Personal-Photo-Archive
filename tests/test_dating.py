"""Phase 6 Slice 1 — intrinsic date-reliability engine.

Read-only, deterministic, conservative. Intrinsic evidence never yields
TRUSTED; provenance is preserved; contradiction never raises confidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from ppa import metadata
from ppa.dating import (
    DateObservation,
    ParseStatus,
    Reliability,
    assess,
    assess_file,
    parse_exif_datetime,
)
from ppa.db import connect
from ppa.scanner import scan_library

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _exif(**kv):
    """Build source-qualified EXIF observations from key->value."""
    return [DateObservation("exif", k, v) for k, v in kv.items()]


# --- parsing -----------------------------------------------------------------


def test_parse_exif_datetime_handles_zeroed_and_bad():
    assert parse_exif_datetime("2004:12:25 09:14:32") == datetime(2004, 12, 25, 9, 14, 32, tzinfo=timezone.utc)
    assert parse_exif_datetime("0000:00:00 00:00:00") is None
    assert parse_exif_datetime("garbage") is None
    assert parse_exif_datetime("") is None


# --- core ratings ------------------------------------------------------------


def test_clean_original_is_probably_valid_not_trusted():
    a = assess(_exif(DateTimeOriginal="2010:06:01 12:00:00"), now=NOW)
    assert a.reliability is Reliability.PROBABLY_VALID
    assert a.candidate_date == datetime(2010, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_matching_original_digitized_is_probably_valid_not_trusted():
    # Repetition by the same camera clock is corroborating, not independent.
    a = assess(_exif(DateTimeOriginal="2010:06:01 12:00:00",
                     DateTimeDigitized="2010:06:01 12:00:00"), now=NOW)
    assert a.reliability is Reliability.PROBABLY_VALID


def test_future_date_is_likely_wrong():
    a = assess(_exif(DateTimeOriginal="2099:05:05 05:05:05"), now=NOW)
    assert a.reliability is Reliability.LIKELY_WRONG


def test_prehistoric_date_is_likely_wrong():
    a = assess(_exif(DateTimeOriginal="1975:01:02 08:00:00"), now=NOW)
    assert a.reliability is Reliability.LIKELY_WRONG


def test_only_filesystem_date_is_questionable():
    a = assess([DateObservation("filesystem", "mtime", "2015-03-04T10:00:00Z")], now=NOW)
    assert a.reliability is Reliability.QUESTIONABLE
    assert a.candidate_date == datetime(2015, 3, 4, 10, 0, 0, tzinfo=timezone.utc)


def test_no_dates_is_unknown():
    a = assess([], now=NOW)
    assert a.reliability is Reliability.UNKNOWN
    assert a.candidate_date is None


def test_digitized_only_without_original_is_questionable():
    a = assess(_exif(DateTimeDigitized="2012:09:09 09:09:09"), now=NOW)
    assert a.reliability is Reliability.QUESTIONABLE


# --- adversarial (the review's list) -----------------------------------------


def test_non_exif_datetimeoriginal_cannot_masquerade_as_exif():
    # An AI/user observation labelled DateTimeOriginal is NOT EXIF evidence.
    a = assess([DateObservation("ai-inference", "DateTimeOriginal", "2010:06:01 12:00:00")], now=NOW)
    assert a.reliability is Reliability.UNKNOWN
    assert all(s.source != "exif" or s.parsed is None for s in a.signals if s.key == "DateTimeOriginal")


def test_non_filesystem_mtime_cannot_masquerade():
    a = assess([DateObservation("user", "mtime", "2015-03-04T10:00:00Z")], now=NOW)
    assert a.reliability is Reliability.UNKNOWN


def test_flat_dict_without_provenance_is_rejected():
    with pytest.raises(TypeError):
        assess({"DateTimeOriginal": "2010:06:01 12:00:00"}, now=NOW)


def test_reset_epoch_is_questionable_not_certain():
    # A reset epoch is suspicion, not proof (a real photo can fall on the day).
    a = assess(_exif(DateTimeOriginal="2001:01:01 12:34:56"), now=NOW)
    assert a.reliability is Reliability.QUESTIONABLE
    assert any("reset" in r for r in a.reasons)


def test_reset_epoch_matching_digitized_is_not_trusted():
    a = assess(_exif(DateTimeOriginal="2001:01:01 12:34:56",
                     DateTimeDigitized="2001:01:01 12:34:56"), now=NOW)
    assert a.reliability is Reliability.QUESTIONABLE


def test_reset_epoch_non_midnight_still_flagged():
    # The old detector required exactly 00:00:00; a running clock after reset
    # (e.g. 00:17:43) must still be treated as suspicious.
    a = assess(_exif(DateTimeOriginal="2000:01:01 00:17:43"), now=NOW)
    assert a.reliability is Reliability.QUESTIONABLE


def test_disagreeing_exif_fields_downgrade():
    a = assess(_exif(DateTimeOriginal="2010:06:01 12:00:00",
                     DateTimeDigitized="2020:01:01 00:00:00"), now=NOW)
    assert a.reliability is Reliability.QUESTIONABLE
    assert any("disagree" in r for r in a.reasons)


def test_mtime_cannot_hide_exif_contradiction():
    a = assess(_exif(DateTimeOriginal="2010:06:01 12:00:00",
                     DateTimeDigitized="2020:01:01 00:00:00")
               + [DateObservation("filesystem", "mtime", "2010-06-01T12:00:00Z")],
               now=NOW)
    assert a.reliability is Reliability.QUESTIONABLE  # NOT promoted


def test_timezoneless_local_time_ahead_of_utc_is_not_future():
    # UTC 00:30, legitimate Brisbane (+10) capture 09:00 — not "the future".
    a = assess(_exif(DateTimeOriginal="2026:01:01 09:00:00"),
               now=datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc))
    assert a.reliability is not Reliability.LIKELY_WRONG


def test_malformed_datetimeoriginal_is_reported_as_malformed_not_absent():
    a = assess(_exif(DateTimeOriginal="garbage", DateTimeDigitized="2012:09:09 09:09:09"), now=NOW)
    # Falls back to Digitized -> QUESTIONABLE, but the malformed Original is
    # recorded as evidence (malformed, not merely missing).
    assert a.reliability is Reliability.QUESTIONABLE
    dto = [s for s in a.signals if s.key == "DateTimeOriginal"]
    assert dto and dto[0].status is ParseStatus.MALFORMED
    assert any("malformed" in r for r in a.reasons)


# --- integration / read-only -------------------------------------------------


def test_engine_never_writes(tmp_path):
    lib = tmp_path / "lib"; lib.mkdir()
    img = lib / "a.jpg"
    im = Image.new("RGB", (40, 30), "red")
    exif = im.getexif()
    exif.get_ifd(ExifTags.IFD.Exif)[0x9003] = "2010:06:01 12:00:00"
    im.save(img, format="JPEG", exif=exif)

    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib)
    metadata.extract_stale(conn)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]

    before = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    rev_before = conn.execute("SELECT current_revision_id FROM files").fetchone()["current_revision_id"]

    a = assess_file(conn, fid, now=NOW)
    assert a.reliability is Reliability.PROBABLY_VALID
    assert a.candidate_date == datetime(2010, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    after = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    rev_after = conn.execute("SELECT current_revision_id FROM files").fetchone()["current_revision_id"]
    assert after == before and rev_after == rev_before
