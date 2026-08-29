"""Phase 14.0 recovery preservation-staging regressions."""
from __future__ import annotations

import json
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
from ppa.recovery_planning import build_recovery_plan, record_recovery_plan_proposal
from ppa.recovery_preservation import (
    RecoveryPreservationError,
    STAGE_MISSING,
    STAGE_PRESERVED,
    build_preservation_plan,
    execute_preservation_stage,
)
from ppa.safe_export import ArchiveOutputSafetyError, safe_export_text
from ppa.scanner import scan_library


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def _recorded_case(tmp_path: Path, *, missing_before_proposal: bool = False):
    library = tmp_path / "library"
    target = library / "target.jpg"
    donor = library / "donor.jpg"
    _img(target, "red")
    _img(donor, "red")
    expected_target = target.read_bytes()
    expected_donor = donor.read_bytes()

    db_path = tmp_path / "catalogue.sqlite3"
    conn = connect(db_path)
    scan_library(conn, library)
    rows = {r["filename"]: r for r in conn.execute("SELECT * FROM files")}

    _img(target, "blue")
    suspect = target.read_bytes()
    assert verify_library(conn).mismatches == 1
    inv = build_mismatch_investigation(
        conn, rows["target.jpg"]["id"], thumbnail_cache_dir=tmp_path / "thumbs"
    )
    decision = plan_mismatch_resolution(
        conn,
        file_id=inv.file_id,
        action=ACTION_RETAIN_EXPECTED,
        reviewed_expected_revision_id=inv.expected_revision_id,
        reviewed_expected_sha256=inv.expected_sha256,
        reviewed_current_state=inv.current_state,
        reviewed_current_sha256=inv.current_observed_sha256,
        reviewed_observation_id=inv.verify_observation_id,
    )
    execute_mismatch_resolution(conn, decision, note="preserve before recovery")
    if missing_before_proposal:
        target.unlink()

    phase13 = build_recovery_plan(conn, file_id=rows["target.jpg"]["id"])
    recorded = record_recovery_plan_proposal(conn, phase13, note="frozen donor proposal")
    return conn, db_path, library, target, donor, rows, expected_target, expected_donor, suspect, recorded


def test_preservation_plan_is_read_only_and_binds_frozen_proposal(tmp_path: Path) -> None:
    conn, db_path, _library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    target_before = target.read_bytes()
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    assert plan.schema == "ppa-recovery-preservation-plan/1"
    assert plan.preservation_required is True
    assert plan.target_observed_sha256
    assert plan.donor_observed_sha256 == plan.expected_sha256
    assert plan.execution_authorized is False
    assert plan.target_replacement_authorized is False
    assert plan.donor_materialization_authorized is False
    assert Path(plan.preservation_root) == db_path.parent / "recovery-preservation"
    assert not Path(plan.preservation_root).exists()
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_before


def test_execute_preservation_stages_exact_suspect_bytes_without_touching_sources(tmp_path: Path) -> None:
    conn, _db_path, library, target, donor, rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    target_before = target.read_bytes()
    target_stat = target.stat()
    donor_stat = donor.stat()
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    result = execute_preservation_stage(conn, plan, note="first preservation checkpoint")

    assert result.stage_state == STAGE_PRESERVED
    preserved = Path(result.preservation_path)
    manifest = Path(result.manifest_path)
    assert preserved.is_file()
    assert preserved.read_bytes() == suspect
    assert result.preserved_sha256 == plan.target_observed_sha256
    assert result.preserved_size_bytes == len(suspect)
    assert manifest.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema"] == "ppa-recovery-preservation-manifest/1"
    assert payload["target_replacement_performed"] is False
    assert payload["donor_materialized"] is False
    assert payload["preserved_sha256"] == result.preserved_sha256
    assert library not in preserved.parents

    # Phase 14.0 writes a preservation copy only. Target and donor objects/bytes remain untouched.
    assert target.read_bytes() == target_before
    assert donor.read_bytes() == donor_before
    assert target.stat().st_ino == target_stat.st_ino
    assert donor.stat().st_ino == donor_stat.st_ino

    row = conn.execute(
        "SELECT * FROM archive_recovery_preservation_stages WHERE stage_id=?", (result.stage_id,)
    ).fetchone()
    assert row["proposal_id"] == recorded.proposal_id
    assert row["target_replacement_performed"] == 0
    assert row["donor_materialized"] == 0
    assert row["recovery_execution_authorized"] == 0
    event = conn.execute(
        "SELECT event_type,detail FROM integrity_events WHERE file_id=? ORDER BY id DESC LIMIT 1",
        (rows["target.jpg"]["id"],),
    ).fetchone()
    assert event["event_type"] == "archive_recovery_preservation_staged"
    assert "not replaced" in event["detail"]


def test_missing_target_records_no_preservation_required(tmp_path: Path) -> None:
    conn, _db, _library, target, donor, _rows, _expected, donor_before, _suspect, recorded = _recorded_case(
        tmp_path, missing_before_proposal=True
    )
    assert not target.exists()
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    assert plan.target_state == "missing"
    assert plan.preservation_required is False
    assert plan.preservation_path is None
    result = execute_preservation_stage(conn, plan)
    assert result.stage_state == STAGE_MISSING
    assert result.preservation_path is None
    assert result.preserved_sha256 is None
    assert Path(result.manifest_path).is_file()
    assert not target.exists()
    assert donor.read_bytes() == donor_before


def test_preservation_execution_rejects_external_target_change_and_leaves_no_stage(tmp_path: Path) -> None:
    conn, db_path, _library, target, donor, _rows, _expected, donor_before, _suspect, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    _img(target, "green")
    with pytest.raises(RecoveryPreservationError, match="stale|changed|no longer"):
        execute_preservation_stage(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0
    root = db_path.parent / "recovery-preservation"
    assert not (root / plan.stage_id).exists()
    assert donor.read_bytes() == donor_before


def test_preservation_execution_rejects_external_donor_change_and_leaves_target_untouched(tmp_path: Path) -> None:
    conn, _db, _library, target, donor, _rows, _expected, _donor_before, suspect, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    _img(donor, "green")
    with pytest.raises(RecoveryPreservationError, match="stale|donor|no longer"):
        execute_preservation_stage(conn, plan)
    assert target.read_bytes() == suspect
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0


def test_preservation_rolls_back_and_discards_stage_if_target_changes_during_copy(tmp_path: Path, monkeypatch) -> None:
    conn, db_path, _library, target, donor, _rows, _expected, donor_before, _suspect, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)

    import ppa.recovery_preservation as rp
    real_copy = rp._copy_preserved_bytes

    def racing_copy(source: Path, temporary: Path):
        result = real_copy(source, temporary)
        _img(target, "green")
        return result

    monkeypatch.setattr(rp, "_copy_preserved_bytes", racing_copy)
    with pytest.raises(RecoveryPreservationError, match="changed"):
        execute_preservation_stage(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0
    assert not (db_path.parent / "recovery-preservation" / plan.stage_id).exists()
    assert donor.read_bytes() == donor_before


def test_preservation_root_inside_source_library_is_rejected(tmp_path: Path) -> None:
    conn, _db, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    with pytest.raises(RecoveryPreservationError, match="source Library"):
        build_preservation_plan(
            conn, proposal_id=recorded.proposal_id,
            preservation_root=library / ".ppa-recovery-preservation",
        )
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_successful_stage_is_one_shot_per_frozen_proposal(tmp_path: Path) -> None:
    conn, _db, _library, _target, _donor, _rows, *_rest, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    execute_preservation_stage(conn, plan)
    with pytest.raises(RecoveryPreservationError, match="already has a preservation stage"):
        build_preservation_plan(conn, proposal_id=recorded.proposal_id)


def test_safe_exports_cannot_overwrite_phase14_operational_preservation_store(tmp_path: Path) -> None:
    conn, db_path, _library, _target, _donor, _rows, *_rest, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    result = execute_preservation_stage(conn, plan)
    preserved = Path(result.preservation_path)
    before = preserved.read_bytes()
    with pytest.raises(ArchiveOutputSafetyError, match="operational"):
        safe_export_text(preserved, "DESTROY", conn=conn)
    assert preserved.read_bytes() == before
    with pytest.raises(ArchiveOutputSafetyError, match="operational"):
        safe_export_text(db_path.parent / "recovery-preservation" / "report.json", "{}", conn=conn)


def test_migration_033_and_stage_rows_are_immutable(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")
    assert current_schema_version(conn) >= 33
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_recovery_preservation_stages'"
    ).fetchone()
    conn.close()

    conn, _db, _library, _target, _donor, _rows, *_rest, recorded = _recorded_case(tmp_path / "case")
    result = execute_preservation_stage(conn, build_preservation_plan(conn, proposal_id=recorded.proposal_id))
    with pytest.raises(Exception, match="immutable"):
        conn.execute(
            "UPDATE archive_recovery_preservation_stages SET note='rewritten' WHERE stage_id=?",
            (result.stage_id,),
        )


def test_recovery_stage_cli_requires_apply_and_then_preserves_without_replacing_target(tmp_path: Path, monkeypatch) -> None:
    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    conn.close()
    cfg_path = tmp_path / "cli-config.toml"
    cfg_path.write_text(
        f'''[database]\npath = "{db_path.as_posix()}"\n[logging]\nlevel = "INFO"\npath = "{(tmp_path / 'cli.log').as_posix()}"\n[library]\ndirectories = ["{library.as_posix()}"]\n''',
        encoding="utf-8",
    )
    import ppa.config as config_mod
    import ppa.cli as cli_mod
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)

    # Without --apply, this is a read-only readiness plan.
    assert cli_mod.main(["recovery-stage-preservation", recorded.proposal_id]) == 0
    assert not (db_path.parent / "recovery-preservation").exists()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before

    report = tmp_path / "stage-result.json"
    assert cli_mod.main([
        "recovery-stage-preservation", recorded.proposal_id, "--apply", "--json", str(report)
    ]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["stage_state"] == STAGE_PRESERVED
    assert Path(payload["preservation_path"]).read_bytes() == suspect
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_preservation_checkpoint_rejects_staged_evidence_tamper_before_commit(tmp_path: Path, monkeypatch) -> None:
    conn, db_path, _library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    import ppa.recovery_preservation as rp
    real_manifest = rp._write_manifest

    def tampering_manifest(path: Path, payload: dict) -> str:
        digest = real_manifest(path, payload)
        preservation = Path(payload["preservation_path"])
        preservation.chmod(0o600)
        preservation.write_bytes(b"tampered-preservation-evidence")
        return digest

    monkeypatch.setattr(rp, "_write_manifest", tampering_manifest)
    with pytest.raises(RecoveryPreservationError, match="preservation copy changed"):
        execute_preservation_stage(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0
    assert not (db_path.parent / "recovery-preservation" / plan.stage_id).exists()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_stage_id_is_canonical_uuid_and_cannot_escape_preservation_root(tmp_path: Path) -> None:
    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    for unsafe in ("../library", "..", ".", "/tmp/escape", "not-a-uuid", "A" * 36):
        with pytest.raises(RecoveryPreservationError, match="stage ID"):
            build_preservation_plan(conn, proposal_id=recorded.proposal_id, stage_id=unsafe)
    assert library.is_dir()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    assert not (db_path.parent / "recovery-preservation").exists()


def test_execute_rejects_forged_traversal_stage_id_before_any_filesystem_write(tmp_path: Path) -> None:
    from dataclasses import replace

    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    forged = replace(plan, stage_id="../library")
    with pytest.raises(RecoveryPreservationError, match="stage ID"):
        execute_preservation_stage(conn, forged)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0
    assert library.is_dir()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    assert not (db_path.parent / "recovery-preservation").exists()


def test_failure_cleanup_does_not_follow_or_chmod_unexpected_symlink(tmp_path: Path, monkeypatch) -> None:
    conn, db_path, _library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    target_mode = target.stat().st_mode

    import ppa.recovery_preservation as rp
    real_copy = rp._copy_preserved_bytes

    def inject_unexpected_alias(source: Path, temporary: Path):
        result = real_copy(source, temporary)
        stage_dir = temporary.parent
        alias = stage_dir / "unexpected-source-alias"
        try:
            alias.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is unavailable in this environment")
        raise RecoveryPreservationError("forced rollback after unexpected alias")

    monkeypatch.setattr(rp, "_copy_preserved_bytes", inject_unexpected_alias)
    with pytest.raises(RecoveryPreservationError, match="forced rollback"):
        execute_preservation_stage(conn, plan)

    stage_dir = db_path.parent / "recovery-preservation" / plan.stage_id
    alias = stage_dir / "unexpected-source-alias"
    # Unknown contents are intentionally left for diagnosis rather than traversed.
    assert stage_dir.is_dir()
    assert alias.is_symlink()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    assert target.stat().st_mode == target_mode
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0


def test_custom_recorded_preservation_root_is_protected_from_safe_exports(tmp_path: Path) -> None:
    conn, _db_path, _library, _target, _donor, _rows, *_rest, recorded = _recorded_case(tmp_path)
    custom_root = tmp_path / "operational" / "ppa-preserved-evidence"
    plan = build_preservation_plan(
        conn,
        proposal_id=recorded.proposal_id,
        preservation_root=custom_root,
    )
    result = execute_preservation_stage(conn, plan)
    assert Path(result.manifest_path).is_file()
    with pytest.raises(ArchiveOutputSafetyError, match="operational"):
        safe_export_text(custom_root / "report.json", "{}", conn=conn)
