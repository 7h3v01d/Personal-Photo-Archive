"""Phase 13.0 dry-run recovery planning and donor qualification regressions."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from ppa.db import connect, current_schema_version
from ppa.integrity import verify_library
from ppa.mismatch_investigation import build_mismatch_investigation
from ppa.mismatch_resolution import (
    ACTION_RETAIN_EXPECTED,
    execute_mismatch_resolution,
    plan_mismatch_resolution,
)
from ppa.recovery_planning import (
    RecoveryPlanningError,
    build_recovery_plan,
    build_recovery_planning_view,
    record_recovery_plan_proposal,
)
from ppa.scanner import scan_library


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (72, 54), color=color).save(path)


def _recovery_case(tmp_path: Path):
    library = tmp_path / "library"
    target = library / "target.jpg"
    donor = library / "donor.jpg"
    _img(target, "red")
    _img(donor, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    rows = {r["filename"]: r for r in conn.execute("SELECT * FROM files")}
    expected_target = target.read_bytes()
    expected_donor = donor.read_bytes()

    _img(target, "blue")
    report = verify_library(conn)
    assert report.mismatches == 1
    target_row = conn.execute("SELECT * FROM files WHERE id=?", (rows["target.jpg"]["id"],)).fetchone()
    assert target_row["health_status"] == "hash_mismatch"

    inv = build_mismatch_investigation(
        conn, rows["target.jpg"]["id"], thumbnail_cache_dir=tmp_path / "thumbs"
    )
    plan = plan_mismatch_resolution(
        conn,
        file_id=inv.file_id,
        action=ACTION_RETAIN_EXPECTED,
        reviewed_expected_revision_id=inv.expected_revision_id,
        reviewed_expected_sha256=inv.expected_sha256,
        reviewed_current_state=inv.current_state,
        reviewed_current_sha256=inv.current_observed_sha256,
        reviewed_observation_id=inv.verify_observation_id,
    )
    resolution = execute_mismatch_resolution(conn, plan, note="Recover from exact copy")
    return conn, rows, target, donor, expected_target, expected_donor, resolution


def test_recovery_view_qualifies_fresh_exact_donor_and_plan_is_dry_run(tmp_path: Path) -> None:
    conn, rows, target, donor, target_before, donor_before, resolution = _recovery_case(tmp_path)
    view = build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])
    assert view.recovery_intent_resolution_id == resolution.resolution_id
    assert view.target_state == "still_mismatched"
    assert len(view.qualified_candidates) == 1
    assert view.preferred_donor_file_id == rows["donor.jpg"]["id"]
    candidate = view.qualified_candidates[0]
    assert candidate.physical_sha256 == view.expected_sha256
    assert candidate.qualified

    recovery = build_recovery_plan(conn, file_id=view.file_id)
    assert recovery.dry_run_only is True
    assert recovery.execution_authorized is False
    assert recovery.donor_file_id == rows["donor.jpg"]["id"]
    assert recovery.independent_backup_claim is False
    assert recovery.evidence_fingerprint
    assert "preserve" in " ".join(recovery.proposed_action).lower()
    assert target.read_bytes() != target_before
    assert donor.read_bytes() == donor_before


def test_recorded_recovery_proposal_is_audited_not_executed(tmp_path: Path) -> None:
    conn, rows, target, donor, _target_before, donor_before, _resolution = _recovery_case(tmp_path)
    plan = build_recovery_plan(conn, file_id=rows["target.jpg"]["id"])
    target_current = target.read_bytes()
    result = record_recovery_plan_proposal(conn, plan, note="dry-run review")
    assert result.proposal_state == "dry_run_not_executed"
    row = conn.execute(
        "SELECT * FROM archive_recovery_plan_proposals WHERE proposal_id=?", (plan.proposal_id,)
    ).fetchone()
    assert row is not None
    assert row["proposal_state"] == "dry_run_not_executed"
    assert row["independent_backup_claim"] == 0
    assert target.read_bytes() == target_current
    assert donor.read_bytes() == donor_before
    event = conn.execute(
        "SELECT event_type,detail FROM integrity_events WHERE file_id=? ORDER BY id DESC LIMIT 1",
        (rows["target.jpg"]["id"],),
    ).fetchone()
    assert event["event_type"] == "archive_recovery_plan_proposed"
    assert "proposed but not executed" in event["detail"]


def test_external_donor_edit_without_verify_is_rejected(tmp_path: Path) -> None:
    conn, rows, _target, donor, *_ = _recovery_case(tmp_path)
    _img(donor, "green")
    view = build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])
    donor_candidate = next(c for c in view.candidates if c.file_id == rows["donor.jpg"]["id"])
    assert donor_candidate.qualified is False
    assert any("physical bytes" in reason for reason in donor_candidate.rejection_reasons)
    with pytest.raises(RecoveryPlanningError, match="no qualified recovery donor"):
        build_recovery_plan(conn, file_id=rows["target.jpg"]["id"])


def test_unhealthy_donor_is_not_read_as_recovery_authority(tmp_path: Path) -> None:
    conn, rows, _target, donor, *_ = _recovery_case(tmp_path)
    conn.execute("UPDATE files SET health_status='unreadable' WHERE id=?", (rows["donor.jpg"]["id"],))
    conn.commit()
    view = build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])
    candidate = next(c for c in view.candidates if c.file_id == rows["donor.jpg"]["id"])
    assert not candidate.qualified
    assert candidate.physical_state is None
    assert any("health is unreadable" in reason for reason in candidate.rejection_reasons)
    # Source remains untouched even though the catalogue was deliberately made unhealthy.
    assert donor.is_file()


def test_ambiguous_origin_donor_is_rejected(tmp_path: Path) -> None:
    conn, rows, _target, _donor, *_ = _recovery_case(tmp_path)
    donor = rows["donor.jpg"]
    lid = int(donor["library_id"])
    conn.execute(
        """
        INSERT INTO file_origin_ambiguities(
            id,library_id,observed_file_id,sha256,observed_path,
            candidate_file_ids_json,candidate_photo_ids_json,ambiguity_kind,created_at,session_id
        ) VALUES ('amb-test',?,?,?,?,?,?,?,'2026-08-28T00:00:00Z',NULL)
        """,
        (
            lid, donor["id"], donor["sha256"], donor["path"],
            '["older-a","older-b"]', '["photo-a"]', 'ambiguous_relocation',
        ),
    )
    conn.commit()
    view = build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])
    candidate = next(c for c in view.candidates if c.file_id == donor["id"])
    assert not candidate.qualified
    assert candidate.origin_ambiguous
    assert any("ambiguous" in reason for reason in candidate.rejection_reasons)


def test_plan_record_revalidates_target_and_donor_evidence(tmp_path: Path) -> None:
    conn, rows, _target, donor, *_ = _recovery_case(tmp_path)
    plan = build_recovery_plan(conn, file_id=rows["target.jpg"]["id"])
    _img(donor, "green")
    with pytest.raises(RecoveryPlanningError, match="stale|qualified|changed|donor"):
        record_recovery_plan_proposal(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_plan_proposals").fetchone()[0] == 0


def test_recovery_planning_requires_latest_recovery_needed_disposition(tmp_path: Path) -> None:
    conn, rows, _target, _donor, *_ = _recovery_case(tmp_path)
    # A later human disposition supersedes the recovery intent.
    inv = build_mismatch_investigation(
        conn, rows["target.jpg"]["id"], thumbnail_cache_dir=tmp_path / "thumbs-later"
    )
    from ppa.mismatch_resolution import ACTION_UNRESOLVED
    later = plan_mismatch_resolution(
        conn,
        file_id=inv.file_id,
        action=ACTION_UNRESOLVED,
        reviewed_expected_revision_id=inv.expected_revision_id,
        reviewed_expected_sha256=inv.expected_sha256,
        reviewed_current_state=inv.current_state,
        reviewed_current_sha256=inv.current_observed_sha256,
        reviewed_observation_id=inv.verify_observation_id,
    )
    execute_mismatch_resolution(conn, later)
    with pytest.raises(RecoveryPlanningError, match="latest human disposition"):
        build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])


def test_recovery_planning_refuses_when_expected_bytes_have_returned(tmp_path: Path) -> None:
    conn, rows, target, _donor, target_before, *_ = _recovery_case(tmp_path)
    target.write_bytes(target_before)
    with pytest.raises(RecoveryPlanningError, match="run Verify"):
        build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])




def test_cross_library_exact_donor_is_visible_and_qualified_without_identity_merge(tmp_path: Path) -> None:
    library_a = tmp_path / "library-a"
    library_b = tmp_path / "library-b"
    target = library_a / "target.jpg"
    donor = library_b / "donor.jpg"
    _img(target, "red"); _img(donor, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library_a)
    scan_library(conn, library_b)
    target_row = conn.execute("SELECT * FROM files WHERE path=?", (str(target.resolve()),)).fetchone()
    donor_row = conn.execute("SELECT * FROM files WHERE path=?", (str(donor.resolve()),)).fetchone()
    assert target_row["library_id"] != donor_row["library_id"]
    target_photo_before = target_row["photo_id"]
    donor_photo_before = donor_row["photo_id"]

    _img(target, "blue")
    verify_library(conn)
    inv = build_mismatch_investigation(conn, target_row["id"], thumbnail_cache_dir=tmp_path / "thumbs-cross")
    decision = plan_mismatch_resolution(
        conn, file_id=inv.file_id, action=ACTION_RETAIN_EXPECTED,
        reviewed_expected_revision_id=inv.expected_revision_id,
        reviewed_expected_sha256=inv.expected_sha256,
        reviewed_current_state=inv.current_state,
        reviewed_current_sha256=inv.current_observed_sha256,
        reviewed_observation_id=inv.verify_observation_id,
    )
    execute_mismatch_resolution(conn, decision)

    view = build_recovery_planning_view(conn, file_id=target_row["id"])
    candidate = next(c for c in view.candidates if c.file_id == donor_row["id"])
    assert candidate.qualified
    assert candidate.same_library is False
    assert candidate.library_id == donor_row["library_id"]
    plan = build_recovery_plan(conn, file_id=target_row["id"], donor_file_id=donor_row["id"])
    assert plan.donor_library_id == donor_row["library_id"]
    assert plan.same_library is False
    # Planning byte recovery must not reinterpret logical identity.
    assert conn.execute("SELECT photo_id FROM files WHERE id=?", (target_row["id"],)).fetchone()[0] == target_photo_before
    assert conn.execute("SELECT photo_id FROM files WHERE id=?", (donor_row["id"],)).fetchone()[0] == donor_photo_before


def test_hardlink_same_object_candidate_is_explicitly_rejected(tmp_path: Path) -> None:
    conn, rows, target, donor, *_ = _recovery_case(tmp_path)
    donor.unlink()
    try:
        donor.hardlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("hard links unavailable on this filesystem")
    view = build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])
    candidate = next(c for c in view.candidates if c.file_id == rows["donor.jpg"]["id"])
    assert not candidate.qualified
    assert candidate.topology_class == "same_filesystem_object"
    assert any("same filesystem object" in reason for reason in candidate.rejection_reasons)


def test_missing_target_produces_restore_plan_without_claiming_topology_independence(tmp_path: Path) -> None:
    conn, rows, target, _donor, *_ = _recovery_case(tmp_path)
    target.unlink()
    view = build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])
    assert view.target_state == "missing"
    assert len(view.qualified_candidates) == 1
    plan = build_recovery_plan(conn, file_id=rows["target.jpg"]["id"])
    assert "restore_missing_destination" in plan.proposed_action[0]
    assert plan.independent_backup_claim is False
    assert plan.topology_class == "target_storage_identity_unavailable"
    assert "preserve the currently" not in " ".join(plan.proposed_action).lower()


def test_migration_032_and_proposal_immutability(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")
    assert current_schema_version(conn) >= 32
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_recovery_plan_proposals'"
    ).fetchone() is not None
    triggers = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='archive_recovery_plan_proposals'"
    )}
    assert "trg_archive_recovery_proposal_immutable" in triggers
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_recorded_proposal_row_cannot_be_updated(tmp_path: Path) -> None:
    import sqlite3
    conn, rows, *_ = _recovery_case(tmp_path)
    plan = build_recovery_plan(conn, file_id=rows["target.jpg"]["id"])
    record_recovery_plan_proposal(conn, plan)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE archive_recovery_plan_proposals SET note='rewritten' WHERE proposal_id=?",
            (plan.proposal_id,),
        )
    conn.rollback()


def test_latest_human_resolution_uses_append_order_not_wall_clock(tmp_path: Path) -> None:
    conn, rows, _target, _donor, *_ = _recovery_case(tmp_path)
    first = conn.execute(
        "SELECT id FROM integrity_mismatch_resolutions WHERE file_id=? ORDER BY id DESC LIMIT 1",
        (rows["target.jpg"]["id"],),
    ).fetchone()["id"]

    inv = build_mismatch_investigation(
        conn, rows["target.jpg"]["id"], thumbnail_cache_dir=tmp_path / "thumbs-clock"
    )
    from ppa.mismatch_resolution import ACTION_UNRESOLVED, latest_mismatch_resolution
    later_plan = plan_mismatch_resolution(
        conn,
        file_id=inv.file_id,
        action=ACTION_UNRESOLVED,
        reviewed_expected_revision_id=inv.expected_revision_id,
        reviewed_expected_sha256=inv.expected_sha256,
        reviewed_current_state=inv.current_state,
        reviewed_current_sha256=inv.current_observed_sha256,
        reviewed_observation_id=inv.verify_observation_id,
    )
    later = execute_mismatch_resolution(conn, later_plan)
    second = conn.execute(
        "SELECT id FROM integrity_mismatch_resolutions WHERE resolution_id=?",
        (later.resolution_id,),
    ).fetchone()["id"]
    assert second > first

    # Simulate RTC/NTP rollback: the causally older decision has the newer timestamp.
    conn.execute("UPDATE integrity_mismatch_resolutions SET resolved_at='2030-01-01T00:00:00Z' WHERE id=?", (first,))
    conn.execute("UPDATE integrity_mismatch_resolutions SET resolved_at='2020-01-01T00:00:00Z' WHERE id=?", (second,))
    conn.commit()

    latest = latest_mismatch_resolution(conn, rows["target.jpg"]["id"])
    assert latest["id"] == second
    assert latest["action"] == ACTION_UNRESOLVED
    refreshed = build_mismatch_investigation(
        conn, rows["target.jpg"]["id"], thumbnail_cache_dir=tmp_path / "thumbs-clock-2"
    )
    assert refreshed.latest_resolution_action == ACTION_UNRESOLVED
    with pytest.raises(RecoveryPlanningError, match="latest human disposition"):
        build_recovery_planning_view(conn, file_id=rows["target.jpg"]["id"])


def test_recovery_cli_json_cannot_overwrite_qualified_donor(tmp_path: Path, monkeypatch) -> None:
    conn, rows, _target, donor, _target_before, donor_before, _resolution = _recovery_case(tmp_path)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    library = donor.parent
    cfg_path = tmp_path / "cli-config.toml"
    cfg_path.write_text(
        f'''[database]\npath = "{db_path.as_posix()}"\n[logging]\nlevel = "INFO"\npath = "{(tmp_path / 'cli.log').as_posix()}"\n[library]\ndirectories = ["{library.as_posix()}"]\n''',
        encoding="utf-8",
    )
    import ppa.config as config_mod
    import ppa.cli as cli_mod
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)

    code = cli_mod.main([
        "recovery-plan", rows["target.jpg"]["id"], "--json", str(donor)
    ])
    assert code == 1
    assert donor.read_bytes() == donor_before
    with Image.open(donor) as image:
        image.verify()
