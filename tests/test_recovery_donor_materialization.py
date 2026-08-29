"""Phase 14.1 verified donor-materialization regressions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from ppa.db import connect, current_schema_version
from ppa.integrity import verify_library
from ppa.mismatch_investigation import build_mismatch_investigation
from ppa.mismatch_resolution import ACTION_RETAIN_EXPECTED, execute_mismatch_resolution, plan_mismatch_resolution
from ppa.recovery_planning import build_recovery_plan, record_recovery_plan_proposal
from ppa.recovery_preservation import build_preservation_plan, execute_preservation_stage
from ppa.recovery_donor_materialization import (
    MATERIALIZED,
    RecoveryDonorMaterializationError,
    build_donor_materialization_plan,
    execute_donor_materialization,
)
from ppa.scanner import scan_library


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def _staged_case(tmp_path: Path, *, missing: bool = False):
    library = tmp_path / "library"
    target = library / "target.jpg"
    donor = library / "donor.jpg"
    _img(target, "red")
    _img(donor, "red")
    donor_expected = donor.read_bytes()
    db_path = tmp_path / "catalogue.sqlite3"
    conn = connect(db_path)
    scan_library(conn, library)
    rows = {r["filename"]: r for r in conn.execute("SELECT * FROM files")}
    _img(target, "blue")
    suspect = target.read_bytes()
    assert verify_library(conn).mismatches == 1
    inv = build_mismatch_investigation(conn, rows["target.jpg"]["id"], thumbnail_cache_dir=tmp_path / "thumbs")
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
    execute_mismatch_resolution(conn, decision)
    if missing:
        target.unlink()
    phase13 = build_recovery_plan(conn, file_id=rows["target.jpg"]["id"])
    proposal = record_recovery_plan_proposal(conn, phase13)
    stage = execute_preservation_stage(conn, build_preservation_plan(conn, proposal_id=proposal.proposal_id))
    return conn, db_path, library, target, donor, rows, donor_expected, suspect, proposal, stage


def test_donor_plan_is_read_only_and_append_only_boundary(tmp_path: Path) -> None:
    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    target_before = target.read_bytes()
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    assert plan.schema == "ppa-recovery-donor-materialization-plan/1"
    assert plan.donor_observed_sha256 == plan.expected_sha256
    assert plan.materialization_authorized is False
    assert plan.target_replacement_authorized is False
    assert plan.recovery_execution_authorized is False
    assert not Path(plan.donor_materialization_path).exists()
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_before


def test_execute_materializes_verified_donor_without_touching_sources(tmp_path: Path) -> None:
    conn, _db, library, target, donor, rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    target_stat = target.stat()
    donor_stat = donor.stat()
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    result = execute_donor_materialization(conn, plan, note="verified expected bytes")
    materialized = Path(result.donor_materialization_path)
    manifest = Path(result.donor_manifest_path)
    assert result.materialization_state == MATERIALIZED
    assert materialized.read_bytes() == donor_before
    assert result.donor_materialized_sha256 == plan.expected_sha256
    assert library not in materialized.parents
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema"] == "ppa-recovery-donor-manifest/1"
    assert payload["target_replacement_performed"] is False
    assert payload["recovery_execution_authorized"] is False
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    assert target.stat().st_ino == target_stat.st_ino
    assert donor.stat().st_ino == donor_stat.st_ino
    row = conn.execute("SELECT * FROM archive_recovery_donor_materializations WHERE stage_id=?", (stage.stage_id,)).fetchone()
    assert row["target_replacement_performed"] == 0
    assert row["recovery_execution_authorized"] == 0
    event = conn.execute("SELECT event_type FROM integrity_events WHERE file_id=? ORDER BY id DESC LIMIT 1", (rows["target.jpg"]["id"],)).fetchone()
    assert event["event_type"] == "archive_recovery_donor_materialized"


def test_missing_target_can_materialize_donor_without_creating_target(tmp_path: Path) -> None:
    conn, _db, _library, target, donor, _rows, donor_before, _suspect, _proposal, stage = _staged_case(tmp_path, missing=True)
    assert not target.exists()
    result = execute_donor_materialization(conn, build_donor_materialization_plan(conn, stage_id=stage.stage_id))
    assert Path(result.donor_materialization_path).read_bytes() == donor_before
    assert donor.read_bytes() == donor_before
    assert not target.exists()


def test_donor_materialization_rejects_external_donor_change(tmp_path: Path) -> None:
    conn, _db, _library, target, donor, _rows, _donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    _img(donor, "green")
    with pytest.raises(RecoveryDonorMaterializationError, match="stale|donor|valid|changed"):
        execute_donor_materialization(conn, plan)
    assert target.read_bytes() == suspect
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0


def test_donor_materialization_rejects_target_change_after_preservation(tmp_path: Path) -> None:
    conn, _db, _library, target, donor, _rows, donor_before, _suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    _img(target, "green")
    with pytest.raises(RecoveryDonorMaterializationError, match="target|stale|valid|evidence changed"):
        execute_donor_materialization(conn, plan)
    assert donor.read_bytes() == donor_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0


def test_materialization_rejects_preservation_evidence_tamper(tmp_path: Path) -> None:
    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    preserved = Path(stage.preservation_path)
    preserved.chmod(0o600)
    preserved.write_bytes(b"tampered")
    with pytest.raises(RecoveryDonorMaterializationError, match="preserved suspect evidence"):
        build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_materialization_rolls_back_owned_outputs_if_donor_changes_during_copy(tmp_path: Path, monkeypatch) -> None:
    conn, _db, _library, target, donor, _rows, _donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    import ppa.recovery_donor_materialization as dm
    real_copy = dm._copy_preserved_bytes
    def racing_copy(source: Path, temporary: Path):
        out = real_copy(source, temporary)
        _img(donor, "green")
        return out
    monkeypatch.setattr(dm, "_copy_preserved_bytes", racing_copy)
    with pytest.raises(RecoveryDonorMaterializationError, match="donor changed"):
        execute_donor_materialization(conn, plan)
    assert not Path(plan.donor_materialization_path).exists()
    assert not Path(plan.donor_manifest_path).exists()
    assert Path(stage.manifest_path).exists()
    assert Path(stage.preservation_path).exists()
    assert target.read_bytes() == suspect


def test_materialization_is_one_shot_per_preservation_stage(tmp_path: Path) -> None:
    conn, *_rest, stage = _staged_case(tmp_path)
    execute_donor_materialization(conn, build_donor_materialization_plan(conn, stage_id=stage.stage_id))
    with pytest.raises(RecoveryDonorMaterializationError, match="already has"):
        build_donor_materialization_plan(conn, stage_id=stage.stage_id)


def test_migration_034_and_materialization_rows_are_immutable(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")
    assert current_schema_version(conn) >= 34
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_recovery_donor_materializations'").fetchone()
    conn.close()
    conn, *_rest, stage = _staged_case(tmp_path / "case")
    result = execute_donor_materialization(conn, build_donor_materialization_plan(conn, stage_id=stage.stage_id))
    with pytest.raises(Exception, match="immutable"):
        conn.execute("UPDATE archive_recovery_donor_materializations SET note='changed' WHERE materialization_id=?", (result.materialization_id,))


def test_donor_materialization_cli_requires_apply(tmp_path: Path, monkeypatch) -> None:
    conn, db_path, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    conn.close()
    cfg_path = tmp_path / "cli-config.toml"
    cfg_path.write_text(
        f'''[database]\npath = "{db_path.as_posix()}"\n[logging]\nlevel = "INFO"\npath = "{(tmp_path / 'cli.log').as_posix()}"\n[library]\ndirectories = ["{library.as_posix()}"]\n''',
        encoding="utf-8",
    )
    import ppa.config as config_mod
    import ppa.cli as cli_mod
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    assert cli_mod.main(["recovery-materialize-donor", stage.stage_id]) == 0
    assert donor.read_bytes() == donor_before
    assert target.read_bytes() == suspect
    report = tmp_path / "donor-result.json"
    assert cli_mod.main(["recovery-materialize-donor", stage.stage_id, "--apply", "--json", str(report)]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["materialization_state"] == MATERIALIZED
    assert Path(payload["donor_materialization_path"]).read_bytes() == donor_before
    assert donor.read_bytes() == donor_before
    assert target.read_bytes() == suspect
