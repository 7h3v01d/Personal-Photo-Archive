"""Phase 7.1 — reconstruction persistence, sticky decisions, and flow.

Read-of-evidence only: reconstruction writes only to its own table, never to
observations or the recorded date. Human decisions are sticky across re-runs.
"""

from __future__ import annotations

from pathlib import Path

from PIL import ExifTags, Image

from ppa import anchors, catalogue, metadata
from ppa.db import connect
from ppa.reconstruct_catalogue import (
    analyse_library_reconstructed,
    confirm_reconstruction,
    list_reconstructions,
    reject_reconstruction,
    reopen_reconstruction,
    store_reconstructions,
)
from ppa.scanner import scan_library


def _jpg(p: Path, dto: str, make="Canon", model="5D", serial="SN-1"):
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (32, 24), "red")
    ex = im.getexif(); ex[0x010F] = make; ex[0x0110] = model
    sub = ex.get_ifd(ExifTags.IFD.Exif); sub[0x9003] = dto
    if serial:
        sub[0xA431] = serial
    im.save(p, format="JPEG", exif=ex)


def _reset_run(tmp_path, n=5):
    lib = tmp_path / "lib"
    for i in range(n):
        _jpg(lib / f"IMG_{201+i:04d}.jpg", f"2001:01:01 00:{i*5:02d}:00")
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    return conn


def _fid(conn, name):
    return conn.execute("SELECT id FROM files WHERE filename = ?", (name,)).fetchone()["id"]


def test_migration_creates_reconstructions_table(tmp_path):
    conn = connect(tmp_path / "c.sqlite3")
    assert conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] >= 10
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(reconstructions)")]
    assert {"file_id", "start_date", "end_date", "confidence", "method", "status"} <= set(cols)


def test_offset_run_proposals_stored(tmp_path):
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    counts = store_reconstructions(conn)
    assert counts["proposed"] == 5
    rows = {r.file_id: r for r in list_reconstructions(conn)}
    anchored = rows[_fid(conn, "IMG_0203.jpg")]
    assert anchored.method == "direct" and str(anchored.start_date) == "2004-12-25"
    others = [r for fid, r in rows.items() if fid != _fid(conn, "IMG_0203.jpg")]
    assert all(r.method == "offset" and str(r.start_date) == "2004-12-25" for r in others)


def test_decisions_are_sticky_across_reruns(tmp_path):
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    store_reconstructions(conn)
    confirm_reconstruction(conn, _fid(conn, "IMG_0203.jpg"))
    reject_reconstruction(conn, _fid(conn, "IMG_0201.jpg"))

    counts = store_reconstructions(conn)              # re-run
    assert counts["skipped_decided"] == 2
    by = {r.file_id: r for r in list_reconstructions(conn)}
    assert by[_fid(conn, "IMG_0203.jpg")].status == "confirmed"
    assert by[_fid(conn, "IMG_0201.jpg")].status == "rejected"
    assert by[_fid(conn, "IMG_0203.jpg")].decided_at is not None


def test_confirm_and_reject_return_false_without_row(tmp_path):
    conn = _reset_run(tmp_path)
    assert confirm_reconstruction(conn, "no-such-file") is False
    assert reject_reconstruction(conn, "no-such-file") is False


def test_reconstruction_is_read_only_wrt_observations(tmp_path):
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    before = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    rev = conn.execute("SELECT current_revision_id FROM files LIMIT 1").fetchone()[0]
    store_reconstructions(conn)
    analyse_library_reconstructed(conn)
    after = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    rev2 = conn.execute("SELECT current_revision_id FROM files LIMIT 1").fetchone()[0]
    assert after == before and rev == rev2
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_list_filters_by_status(tmp_path):
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    store_reconstructions(conn)
    confirm_reconstruction(conn, _fid(conn, "IMG_0203.jpg"))
    assert len(list_reconstructions(conn, status="confirmed")) == 1
    assert len(list_reconstructions(conn, status="proposed")) == 4


def test_forgetting_files_cascades_to_reconstructions(tmp_path):
    # reconstructions.file_id has ON DELETE CASCADE, so removing a library's files
    # cleans its reconstructions too.
    conn = _reset_run(tmp_path)
    anchors.add_anchor(conn, "file", _fid(conn, "IMG_0203.jpg"), "exact", "2004-12-25")
    store_reconstructions(conn)
    lid = catalogue.list_libraries(conn)[0].id
    catalogue.forget_library(conn, lid)
    assert list_reconstructions(conn) == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- Phase 7.1.1: revision-bound authority, terminal decisions, provenance ---

import pytest  # noqa: E402


def _replace_bytes(tmp_path, name, dto, color):
    _jpg(tmp_path / "lib" / name, dto, serial="SN-1")
    # overwrite with new content/date -> new SHA -> new revision
    p = tmp_path / "lib" / name
    im = Image.new("RGB", (80, 60), color)
    ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
    s = ex.get_ifd(ExifTags.IFD.Exif); s[0x9003] = dto; s[0xA431] = "SN-1"
    im.save(p, format="JPEG", exif=ex, quality=95)


def _one_file(tmp_path, dto="2001:01:01 00:00:00", color="red"):
    lib = tmp_path / "lib"
    p = lib / "IMG_0201.jpg"; p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (64, 48), color)
    ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
    s = ex.get_ifd(ExifTags.IFD.Exif); s[0x9003] = dto; s[0xA431] = "SN-1"
    im.save(p, format="JPEG", exif=ex, quality=95)
    conn = connect(tmp_path / "c.sqlite3")
    scan_library(conn, lib); metadata.extract_stale(conn)
    return conn


def _bump_revision(conn, tmp_path, dto="2018:07:04 12:00:00", color="blue"):
    p = tmp_path / "lib" / "IMG_0201.jpg"
    im = Image.new("RGB", (80, 60), color)
    ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
    s = ex.get_ifd(ExifTags.IFD.Exif); s[0x9003] = dto; s[0xA431] = "SN-1"
    im.save(p, format="JPEG", exif=ex, quality=95)
    scan_library(conn, tmp_path / "lib"); metadata.extract_stale(conn)


def test_confirmed_reconstruction_does_not_transfer_to_new_revision(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    assert list_reconstructions(conn)[0].stale is False

    _bump_revision(conn, tmp_path)
    store_reconstructions(conn)                       # sticky, but now stale
    r = list_reconstructions(conn)[0]
    assert r.status == "confirmed" and r.stale is True  # no longer authoritative


def test_stale_proposal_cannot_be_confirmed(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn)                        # proposed, bound to rev1
    _bump_revision(conn, tmp_path)                     # rev2 current, proposal stale
    with pytest.raises(ValueError):
        confirm_reconstruction(conn, fid)


def test_confirmed_decision_remains_visible_after_going_stale(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    _bump_revision(conn, tmp_path)
    rows = list_reconstructions(conn)
    assert len(rows) == 1 and rows[0].status == "confirmed"  # preserved, not deleted
    assert str(rows[0].start_date) == "2004-12-25"


def test_rejected_cannot_silently_become_confirmed(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); reject_reconstruction(conn, fid)
    with pytest.raises(ValueError):
        confirm_reconstruction(conn, fid)


def test_confirmed_cannot_silently_become_rejected(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    with pytest.raises(ValueError):
        reject_reconstruction(conn, fid)


def test_reopen_allows_revisiting_a_decision(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); reject_reconstruction(conn, fid)
    assert reopen_reconstruction(conn, fid) is True
    assert list_reconstructions(conn)[0].status == "proposed"
    confirm_reconstruction(conn, fid)                 # now allowed again
    assert list_reconstructions(conn)[0].status == "confirmed"


def test_rerun_preserves_created_at_and_updates_updated_at(tmp_path):
    import time
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn)
    r1 = list_reconstructions(conn)[0]
    time.sleep(0.01)
    store_reconstructions(conn)                        # recompute
    r2 = list_reconstructions(conn)[0]
    assert r2.created_at == r1.created_at              # birth preserved
    assert r2.updated_at >= r1.updated_at              # recompute tracked
    assert r2.engine_version == "7.0.1"


def test_reopen_returns_false_without_decided_row(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn)                        # proposed, not decided
    assert reopen_reconstruction(conn, fid) is False


# --- Phase 7.1.2: evidence-fingerprint binding -------------------------------


def test_confirmed_becomes_stale_when_newer_anchor_supersedes(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    assert list_reconstructions(conn)[0].evidence_stale is False

    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")   # newer, bytes same
    store_reconstructions(conn)
    r = list_reconstructions(conn)[0]
    assert r.status == "confirmed" and r.content_stale is False
    assert r.evidence_stale is True                                # evidence changed


def test_stale_proposal_cannot_be_confirmed_after_anchor_change(tmp_path):
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn)                                    # proposed w/ fp1
    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")   # evidence changes, no re-run
    with pytest.raises(ValueError):
        confirm_reconstruction(conn, fid)                          # refused: evidence-stale


def test_unchanged_evidence_yields_same_fingerprint(tmp_path):
    from ppa.reconstruct_catalogue import _build_inputs, _fingerprints
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    fp1 = _fingerprints(_build_inputs(conn)[0])
    fp2 = _fingerprints(_build_inputs(conn)[0])
    assert fp1 == fp2 and fp1[fid]                                 # deterministic, non-empty


def test_unrelated_catalogue_change_does_not_invalidate(tmp_path):
    # Adding an anchor in a DIFFERENT library must not evidence-stale this file.
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)

    other = tmp_path / "other"
    _jpg(other / "OTHER.jpg", "2010:01:01 00:00:00")
    scan_library(conn, other); metadata.extract_stale(conn)
    ofid = conn.execute("SELECT id FROM files WHERE filename='OTHER.jpg'").fetchone()["id"]
    anchors.add_anchor(conn, "file", ofid, "exact", "2010-01-01")
    store_reconstructions(conn)

    r = next(x for x in list_reconstructions(conn) if x.file_id == fid)
    assert r.content_stale is False and r.evidence_stale is False  # untouched


def test_reopened_row_needs_recompute_before_confirm(tmp_path):
    # After reopen + evidence change, confirming without re-running is refused.
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); reject_reconstruction(conn, fid)
    reopen_reconstruction(conn, fid)
    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")   # evidence moved on
    with pytest.raises(ValueError):
        confirm_reconstruction(conn, fid)                          # must re-run first
    store_reconstructions(conn)                                    # refresh
    confirm_reconstruction(conn, fid)                              # now allowed
    assert list_reconstructions(conn)[0].status == "confirmed"


# --- Phase 7.1.2a: recorded is part of the semantic input --------------------


def test_fingerprint_distinguishes_recorded_candidate(tmp_path):
    from datetime import date, datetime, timezone
    from ppa.dating import Reliability
    from ppa.reconstruct import KnownTrueKind, ReconstructionInput
    from ppa.reconstruct_catalogue import _fingerprints

    def _dt(d):
        return datetime(2001, 1, d, tzinfo=timezone.utc)

    def run(recorded_day):
        return [
            ReconstructionInput("anchor", _dt(1), Reliability.LIKELY_WRONG, "R1", True, 201,
                                known_true=date(2004, 12, 25),
                                known_true_kind=KnownTrueKind.HUMAN_EXACT),
            ReconstructionInput("target", _dt(recorded_day), Reliability.LIKELY_WRONG,
                                "R1", True, 202)]
    # identical except the target's recorded instant -> different fingerprints
    assert _fingerprints(run(1))["target"] != _fingerprints(run(2))["target"]
    assert _fingerprints(run(1))["target"] == _fingerprints(run(1))["target"]


def test_recorded_change_makes_confirmed_row_evidence_stale(tmp_path):
    # Same FileRevision, same anchor, but the interpreted candidate changes (as a
    # re-extraction / parser fix would): the confirmed decision must go stale.
    conn = _one_file(tmp_path)
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn); confirm_reconstruction(conn, fid)
    assert list_reconstructions(conn)[0].evidence_stale is False

    # Re-interpret the SAME bytes to a different capture day (revision unchanged).
    rev = conn.execute("SELECT current_revision_id FROM files WHERE id=?", (fid,)).fetchone()[0]
    conn.execute(
        "UPDATE metadata_observations SET value = '2001:01:02 00:00:00' "
        "WHERE file_id = ? AND key = 'DateTimeOriginal' AND file_revision_id = ?",
        (fid, rev))
    conn.commit()

    r = list_reconstructions(conn)[0]
    assert r.content_stale is False          # bytes/revision unchanged
    assert r.evidence_stale is True          # interpreted input changed


def test_reset_group_renumbering_does_not_change_fingerprint(tmp_path):
    # The reset-group label is an enumeration artefact; only actual membership and
    # evidence are semantic. Renumbering must NOT evidence-stale a decision.
    from datetime import date, datetime, timezone
    from ppa.dating import Reliability
    from ppa.reconstruct import KnownTrueKind, ReconstructionInput
    from ppa.reconstruct_catalogue import _fingerprints

    def _dt():
        return datetime(2001, 1, 1, tzinfo=timezone.utc)

    def group(label):
        return [
            ReconstructionInput("A", _dt(), Reliability.LIKELY_WRONG, label, True, 201,
                                known_true=date(2004, 12, 25),
                                known_true_kind=KnownTrueKind.HUMAN_EXACT),
            ReconstructionInput("B", _dt(), Reliability.LIKELY_WRONG, label, True, 202)]
    f0 = _fingerprints(group("reset-0"))
    f8 = _fingerprints(group("reset-8"))
    assert f0["A"] == f8["A"] and f0["B"] == f8["B"]

    # But a real change to a member's evidence still re-fingerprints the group.
    def group_moved():
        return [
            ReconstructionInput("A", _dt(), Reliability.LIKELY_WRONG, "reset-0", True, 201,
                                known_true=date(2005, 12, 25),
                                known_true_kind=KnownTrueKind.HUMAN_EXACT),
            ReconstructionInput("B", _dt(), Reliability.LIKELY_WRONG, "reset-0", True, 202)]
    assert _fingerprints(group_moved())["B"] != f0["B"]
