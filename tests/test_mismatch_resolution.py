"""Phase 12.4 — controlled, stale-safe hash-mismatch resolution."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from ppa.catalogue import forget_library
from ppa.db import connect, current_schema_version
from ppa.integrity import verify_library
from ppa.mismatch_investigation import build_mismatch_investigation
from ppa.mismatch_resolution import (
    ACTION_ADOPT_CURRENT,
    ACTION_RETAIN_EXPECTED,
    ACTION_UNRESOLVED,
    MISMATCH_RESOLUTION_PLAN_SCHEMA,
    execute_mismatch_resolution,
    plan_mismatch_resolution,
)
from ppa.scanner import scan_library


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def _setup_mismatch(tmp_path: Path):
    library = tmp_path / "library"
    image = library / "one.jpg"
    _img(image, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    report = scan_library(conn, library)
    library_id = conn.execute("SELECT id FROM libraries").fetchone()["id"]
    row = conn.execute(
        "SELECT f.id,f.photo_id,f.current_revision_id,r.sha256 FROM files f "
        "JOIN file_revisions r ON r.id=f.current_revision_id"
    ).fetchone()
    _img(image, "blue")
    verify_library(conn)
    inv = build_mismatch_investigation(conn, row["id"], thumbnail_cache_dir=tmp_path / "thumbs")
    assert inv.current_state == "still_mismatched"
    return conn, library, library_id, image, row, inv


def _plan(conn, inv, action):
    return plan_mismatch_resolution(
        conn,
        file_id=inv.file_id,
        action=action,
        reviewed_expected_revision_id=inv.expected_revision_id,
        reviewed_expected_sha256=inv.expected_sha256,
        reviewed_current_state=inv.current_state,
        reviewed_current_sha256=inv.current_observed_sha256,
        reviewed_observation_id=inv.verify_observation_id,
    )


def test_adopt_current_appends_revision_without_writing_source(tmp_path: Path) -> None:
    conn, _library, _library_id, image, before, inv = _setup_mismatch(tmp_path)
    source_before = image.read_bytes()
    plan = _plan(conn, inv, ACTION_ADOPT_CURRENT)
    assert plan.schema == MISMATCH_RESOLUTION_PLAN_SCHEMA

    result = execute_mismatch_resolution(conn, plan, note="Intentional edit; keep as same photo")
    assert image.read_bytes() == source_before
    assert result.action == ACTION_ADOPT_CURRENT
    assert result.adopted_revision_id is not None
    assert result.adopted_sha256 == inv.current_observed_sha256

    f = conn.execute(
        "SELECT current_revision_id,sha256,health_status,camera_id FROM files WHERE id=?",
        (before["id"],),
    ).fetchone()
    assert f["current_revision_id"] == result.adopted_revision_id
    assert f["sha256"] == inv.current_observed_sha256
    assert f["health_status"] == "ok"
    assert f["camera_id"] is None

    old = conn.execute("SELECT sha256,superseded_at FROM file_revisions WHERE id=?", (before["current_revision_id"],)).fetchone()
    new = conn.execute("SELECT * FROM file_revisions WHERE id=?", (result.adopted_revision_id,)).fetchone()
    assert old["sha256"] == before["sha256"]
    assert old["superseded_at"] is not None
    assert new["sha256"] == inv.current_observed_sha256
    assert new["extraction_status"] == "pending"
    assert conn.execute(
        "SELECT 1 FROM metadata_observations WHERE file_revision_id=? AND source='filesystem' AND key='mtime'",
        (result.adopted_revision_id,),
    ).fetchone() is not None

    audit = conn.execute("SELECT * FROM integrity_mismatch_resolutions WHERE resolution_id=?", (result.resolution_id,)).fetchone()
    assert audit["expected_revision_id"] == before["current_revision_id"]
    assert audit["expected_sha256"] == before["sha256"]
    assert audit["adopted_revision_id"] == result.adopted_revision_id
    assert audit["adopted_sha256"] == inv.current_observed_sha256
    assert audit["note"] == "Intentional edit; keep as same photo"
    assert conn.execute(
        "SELECT 1 FROM integrity_events WHERE file_id=? AND event_type='hash_mismatch_resolved_adopted'",
        (before["id"],),
    ).fetchone() is not None


def test_retain_expected_records_recovery_needed_but_keeps_machine_mismatch(tmp_path: Path) -> None:
    conn, library, _library_id, image, before, inv = _setup_mismatch(tmp_path)
    source_before = image.read_bytes()
    result = execute_mismatch_resolution(conn, _plan(conn, inv, ACTION_RETAIN_EXPECTED), note="Restore from backup later")
    assert image.read_bytes() == source_before
    assert result.adopted_revision_id is None

    f = conn.execute("SELECT current_revision_id,sha256,health_status FROM files WHERE id=?", (before["id"],)).fetchone()
    assert f["current_revision_id"] == before["current_revision_id"]
    assert f["sha256"] == before["sha256"]
    assert f["health_status"] == "hash_mismatch"
    audit = conn.execute("SELECT action,note FROM integrity_mismatch_resolutions WHERE resolution_id=?", (result.resolution_id,)).fetchone()
    assert audit["action"] == ACTION_RETAIN_EXPECTED
    assert audit["note"] == "Restore from backup later"

    # The next ordinary scan must still respect Verify's poisoned fast path;
    # a human recovery-needed decision is not permission to adopt current bytes.
    scan_library(conn, library)
    f2 = conn.execute("SELECT current_revision_id,sha256,health_status FROM files WHERE id=?", (before["id"],)).fetchone()
    assert dict(f2) == dict(f)


def test_unresolved_review_is_append_only_and_changes_no_authority(tmp_path: Path) -> None:
    conn, _library, _library_id, image, before, inv = _setup_mismatch(tmp_path)
    source_before = image.read_bytes()
    result = execute_mismatch_resolution(conn, _plan(conn, inv, ACTION_UNRESOLVED))
    assert image.read_bytes() == source_before
    f = conn.execute("SELECT current_revision_id,sha256,health_status FROM files WHERE id=?", (before["id"],)).fetchone()
    assert f["current_revision_id"] == before["current_revision_id"]
    assert f["sha256"] == before["sha256"]
    assert f["health_status"] == "hash_mismatch"
    assert conn.execute(
        "SELECT action FROM integrity_mismatch_resolutions WHERE resolution_id=?", (result.resolution_id,)
    ).fetchone()["action"] == ACTION_UNRESOLVED


def test_plan_rejects_bytes_changed_since_forensic_review(tmp_path: Path) -> None:
    conn, _library, _library_id, image, before, inv = _setup_mismatch(tmp_path)
    _img(image, "green")
    with pytest.raises(ValueError, match="current on-disk bytes changed"):
        _plan(conn, inv, ACTION_ADOPT_CURRENT)
    assert conn.execute("SELECT current_revision_id FROM files WHERE id=?", (before["id"],)).fetchone()[0] == before["current_revision_id"]


def test_execute_rejects_plan_if_bytes_change_after_planning(tmp_path: Path) -> None:
    conn, _library, _library_id, image, before, inv = _setup_mismatch(tmp_path)
    plan = _plan(conn, inv, ACTION_ADOPT_CURRENT)
    _img(image, "green")
    with pytest.raises(ValueError, match="stale"):
        execute_mismatch_resolution(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM integrity_mismatch_resolutions").fetchone()[0] == 0
    assert conn.execute("SELECT current_revision_id FROM files WHERE id=?", (before["id"],)).fetchone()[0] == before["current_revision_id"]


def test_plan_rejects_newer_verify_observation_than_review(tmp_path: Path) -> None:
    conn, _library, _library_id, _image, _before, inv = _setup_mismatch(tmp_path)
    verify_library(conn)  # same bytes, but a newer structured mismatch observation
    with pytest.raises(ValueError, match="newer mismatch evidence"):
        _plan(conn, inv, ACTION_RETAIN_EXPECTED)


def test_adoption_rejects_unreadable_current_bytes_but_retain_is_allowed(tmp_path: Path) -> None:
    conn, _library, _library_id, image, _before, inv = _setup_mismatch(tmp_path)
    image.write_bytes(b"not a decodable photo anymore")
    inv2 = build_mismatch_investigation(conn, inv.file_id, thumbnail_cache_dir=tmp_path / "thumbs2")
    assert inv2.current_state == "unreadable"
    with pytest.raises(ValueError, match="stable, decodable image"):
        _plan(conn, inv2, ACTION_ADOPT_CURRENT)
    result = execute_mismatch_resolution(conn, _plan(conn, inv2, ACTION_RETAIN_EXPECTED))
    assert result.action == ACTION_RETAIN_EXPECTED


def test_resolution_refuses_when_current_bytes_have_returned_to_expected(tmp_path: Path) -> None:
    conn, _library, _library_id, image, before, _inv = _setup_mismatch(tmp_path)
    # Recreate the exact expected content without running Verify; the health flag
    # deliberately remains stale until Verify re-establishes it.
    _img(image, "red")
    inv2 = build_mismatch_investigation(conn, before["id"], thumbnail_cache_dir=tmp_path / "thumbs2")
    assert inv2.current_state == "matches_expected"
    with pytest.raises(ValueError, match="run Verify"):
        _plan(conn, inv2, ACTION_RETAIN_EXPECTED)


def test_investigation_exposes_latest_recorded_disposition(tmp_path: Path) -> None:
    conn, _library, _library_id, _image, _before, inv = _setup_mismatch(tmp_path)
    execute_mismatch_resolution(conn, _plan(conn, inv, ACTION_UNRESOLVED), note="Need family input")
    refreshed = build_mismatch_investigation(conn, inv.file_id, thumbnail_cache_dir=tmp_path / "thumbs")
    assert refreshed.latest_resolution_action == ACTION_UNRESOLVED
    assert refreshed.latest_resolution_at is not None
    assert refreshed.latest_resolution_note == "Need family input"


def test_migration_030_and_forget_library_cleanup(tmp_path: Path) -> None:
    conn, _library, library_id, _image, _before, inv = _setup_mismatch(tmp_path)
    assert current_schema_version(conn) >= 30
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='integrity_mismatch_resolutions'"
    ).fetchone() is not None
    execute_mismatch_resolution(conn, _plan(conn, inv, ACTION_ADOPT_CURRENT))
    assert conn.execute("SELECT COUNT(*) FROM integrity_mismatch_resolutions").fetchone()[0] == 1
    forget_library(conn, library_id)
    assert conn.execute("SELECT COUNT(*) FROM integrity_mismatch_resolutions").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_revalidation_detects_source_change_during_hash_decode_cycle(tmp_path: Path, monkeypatch) -> None:
    conn, _library, _library_id, image, _before, inv = _setup_mismatch(tmp_path)
    import ppa.mismatch_resolution as mr

    real_sha = mr.sha256_file
    calls = 0

    def changing_sha(path: Path) -> str:
        nonlocal calls
        calls += 1
        digest = real_sha(path)
        if calls == 1:
            _img(image, "green")
        return digest

    monkeypatch.setattr(mr, "sha256_file", changing_sha)
    with pytest.raises(ValueError, match="changed while it was being revalidated"):
        _plan(conn, inv, ACTION_ADOPT_CURRENT)
    assert conn.execute("SELECT COUNT(*) FROM integrity_mismatch_resolutions").fetchone()[0] == 0


def test_non_authority_resolution_plan_is_one_shot(tmp_path: Path) -> None:
    conn, _library, _library_id, _image, _before, inv = _setup_mismatch(tmp_path)
    plan = _plan(conn, inv, ACTION_UNRESOLVED)
    first = execute_mismatch_resolution(conn, plan)
    assert first.action == ACTION_UNRESOLVED
    with pytest.raises(ValueError, match="already been executed"):
        execute_mismatch_resolution(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM integrity_mismatch_resolutions").fetchone()[0] == 1


def test_migration_031_adds_unique_decision_identity(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")
    assert current_schema_version(conn) >= 31
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(integrity_mismatch_resolutions)")}
    assert "decision_id" in cols
    indexes = {r["name"]: int(r["unique"]) for r in conn.execute("PRAGMA index_list(integrity_mismatch_resolutions)")}
    assert indexes["idx_mismatch_resolutions_decision_id"] == 1
    triggers = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='integrity_mismatch_resolutions'"
    )}
    assert "trg_mismatch_resolutions_decision_id_required" in triggers
    assert "trg_mismatch_resolutions_decision_id_immutable" in triggers
