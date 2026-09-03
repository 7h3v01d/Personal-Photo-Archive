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
from ppa.safe_export import ArchiveOutputSafetyError, enroll_export_root, safe_export_text
from ppa.scanner import scan_library
from ppa.secure_write import descriptor_bound_directory_mutation_available


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def _recorded_case(
    tmp_path: Path,
    *,
    missing_before_proposal: bool = False,
    target_name: str = "target.jpg",
    donor_name: str = "donor.jpg",
    extra_source_dir: str | None = None,
):
    library = tmp_path / "library"
    target = library / target_name
    donor = library / donor_name
    _img(target, "red")
    _img(donor, "red")
    if extra_source_dir is not None:
        (library / extra_source_dir).mkdir(parents=True, exist_ok=True)
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
        conn, rows[target_name]["id"], thumbnail_cache_dir=tmp_path / "thumbs"
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

    phase13 = build_recovery_plan(conn, file_id=rows[target_name]["id"])
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
    stage_dir = db_path.parent / "recovery-preservation" / plan.stage_id
    if descriptor_bound_directory_mutation_available():
        # Phase 14.1.16 removes POSIX stat(name)->unlink(name) temporary cleanup.
        # A failed pre-install preservation copy may therefore leave only its
        # PPA-created pending file as recoverable operational debris.
        assert stage_dir.is_dir()
        leftovers = list(stage_dir.iterdir())
        assert leftovers
        assert all(p.is_file() and p.name.endswith(".pending") for p in leftovers)
        assert not (stage_dir / "manifest.json").exists()
    else:
        # On Windows the failed stage is intentionally stranded rather than
        # deleted through pathname authority; source files remain untouched by PPA.
        assert stage_dir.is_dir()
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
    enroll_export_root(tmp_path, conn=conn)
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

    def tampering_manifest(path: Path, payload: dict, *_args) -> str:
        digest = real_manifest(path, payload, *_args)
        preservation = Path(payload["preservation_path"])
        preservation.chmod(0o600)
        preservation.write_bytes(b"tampered-preservation-evidence")
        return digest

    monkeypatch.setattr(rp, "_write_manifest", tampering_manifest)
    with pytest.raises(RecoveryPreservationError, match="preservation copy changed"):
        execute_preservation_stage(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0
    stage_dir = db_path.parent / "recovery-preservation" / plan.stage_id
    # Phase 14.1.17 retains failed stage debris on POSIX too: a later rmdir by
    # child name cannot be bound to the exact directory object that was checked.
    assert stage_dir.is_dir()
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


def test_preservation_temp_hardlink_substitution_cannot_overwrite_trusted_donor(tmp_path: Path, monkeypatch) -> None:
    """The secured pending path may be swapped, but writes remain bound to its original descriptor."""
    import os
    import ppa.recovery_preservation as rp

    conn, _db, _library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    real_copy = rp._copy_preserved_bytes

    def substitute_pending(source: Path, temporary):
        try:
            temporary.path.unlink()
            os.link(donor, temporary.path)
        except (OSError, NotImplementedError):
            pytest.skip("hard-link substitution unavailable in this environment")
        return real_copy(source, temporary)

    monkeypatch.setattr(rp, "_copy_preserved_bytes", substitute_pending)
    with pytest.raises(RecoveryPreservationError, match="temporary identity"):
        execute_preservation_stage(conn, plan)
    assert donor.read_bytes() == donor_before
    assert target.read_bytes() == suspect
    with Image.open(donor) as image:
        image.verify()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0


def test_recorded_preservation_stage_cannot_be_deleted(tmp_path: Path) -> None:
    import sqlite3
    conn, _db, _library, _target, _donor, _rows, *_rest, recorded = _recorded_case(tmp_path)
    result = execute_preservation_stage(conn, build_preservation_plan(conn, proposal_id=recorded.proposal_id))
    with pytest.raises(sqlite3.IntegrityError, match="append-only|cannot be deleted"):
        conn.execute("DELETE FROM archive_recovery_preservation_stages WHERE stage_id=?", (result.stage_id,))
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages WHERE stage_id=?", (result.stage_id,)).fetchone()[0] == 1


def test_postcommit_interrupt_preserves_committed_preservation_evidence(tmp_path: Path, monkeypatch) -> None:
    """A post-COMMIT interruption must never trigger rollback cleanup."""
    conn, db_path, _library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)

    import ppa.recovery_preservation as rp

    calls = {"n": 0}

    def interrupt_postcommit(_path: Path, _identity, _mode: int) -> None:
        calls["n"] += 1
        raise KeyboardInterrupt()

    monkeypatch.setattr(rp, "_chmod_mode_if_same", interrupt_postcommit)
    with pytest.raises(KeyboardInterrupt):
        execute_preservation_stage(conn, plan)

    row = conn.execute(
        "SELECT * FROM archive_recovery_preservation_stages WHERE stage_id=?",
        (plan.stage_id,),
    ).fetchone()
    assert row is not None
    stage_dir = db_path.parent / "recovery-preservation" / plan.stage_id
    assert stage_dir.is_dir()
    assert Path(row["manifest_path"]).is_file()
    assert Path(row["preservation_path"]).is_file()
    assert Path(row["preservation_path"]).read_bytes() == suspect
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_preservation_rollback_cleanup_retains_posix_stage_debris(tmp_path: Path, monkeypatch) -> None:
    """14.1.17: failed POSIX stage cleanup never calls child-name unlink."""
    import ppa.recovery_preservation as rp
    from ppa.secure_write import BoundDirectory

    conn, db_path, _library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(
        tmp_path, target_name="suspect-source.jpg"
    )
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)

    def fail_manifest(path: Path, payload: dict, *_args) -> str:
        assert Path(payload["preservation_path"]).is_file()
        raise RecoveryPreservationError("injected manifest failure")

    def forbidden_unlink(self: BoundDirectory, name: str) -> bool:
        raise AssertionError(f"POSIX rollback must not unlink child by name: {name}")

    monkeypatch.setattr(rp, "_write_manifest", fail_manifest)
    monkeypatch.setattr(BoundDirectory, "unlink_child", forbidden_unlink)

    with pytest.raises(RecoveryPreservationError, match="injected manifest failure"):
        execute_preservation_stage(conn, plan)

    stage_dir = db_path.parent / "recovery-preservation" / plan.stage_id
    assert stage_dir.is_dir()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0



def test_preservation_manifest_parent_real_directory_substitution_cannot_touch_library(tmp_path: Path, monkeypatch) -> None:
    """Phase-14.0 manifest writer must retain the authorised stage-directory object."""
    import os
    import ppa.recovery_preservation as rp

    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    source_manifest = library / "manifest.json"
    source_manifest.write_bytes(b"USER-SOURCE-MANIFEST")
    target_before = target.read_bytes()
    donor_source_before = donor.read_bytes()
    source_manifest_before = source_manifest.read_bytes()
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    stage_path = db_path.parent / "recovery-preservation" / plan.stage_id
    parked = stage_path.with_name(stage_path.name + ".parked")
    real_manifest = rp._write_manifest
    attacked = {"done": False}

    def swapping_manifest(path: Path, payload: dict, *args) -> str:
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(stage_path, parked)
            os.rename(library, stage_path)
        try:
            return real_manifest(path, payload, *args)
        finally:
            if stage_path.exists() and not library.exists():
                os.rename(stage_path, library)
            if parked.exists():
                os.rename(parked, stage_path)

    monkeypatch.setattr(rp, "_write_manifest", swapping_manifest)
    with pytest.raises(RecoveryPreservationError, match="manifest|temporary|identity|parent"):
        execute_preservation_stage(conn, plan)

    assert attacked["done"] is True
    assert target.read_bytes() == target_before
    assert donor.read_bytes() == donor_source_before == donor_before
    assert source_manifest.read_bytes() == source_manifest_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0


def test_preservation_copy_parent_real_directory_substitution_cannot_touch_library(tmp_path: Path, monkeypatch) -> None:
    """The first Phase-14.0 temp write must bind the already-authorised stage object."""
    import os
    import ppa.recovery_preservation as rp

    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    collision = library / "suspect-source.attack.pending"
    collision.write_bytes(b"USER-DATA")
    collision_before = collision.read_bytes()
    target_before = target.read_bytes()
    donor_source_before = donor.read_bytes()
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    stage_path = db_path.parent / "recovery-preservation" / plan.stage_id
    parked = stage_path.with_name(stage_path.name + ".parked")
    real_create = rp.BoundTemporaryFile.create
    attacked = {"done": False}

    def swapping_create(parent, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(stage_path, parked)
            os.rename(library, stage_path)
        try:
            return real_create(parent, *args, **kwargs)
        finally:
            if stage_path.exists() and not library.exists():
                os.rename(stage_path, library)
            if parked.exists():
                os.rename(parked, stage_path)

    monkeypatch.setattr(rp.BoundTemporaryFile, "create", staticmethod(swapping_create))
    with pytest.raises(RecoveryPreservationError, match="temporary|identity|parent|expected|path"):
        execute_preservation_stage(conn, plan)

    assert attacked["done"] is True
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_source_before == donor_before
    assert collision.read_bytes() == collision_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0


def test_preservation_root_bootstrap_substitution_cannot_create_stage_in_library(tmp_path: Path, monkeypatch) -> None:
    """Swap the Library in before root binding: no UUID stage may be created in source data."""
    import os
    import ppa.recovery_preservation as rp

    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    root = db_path.parent / "recovery-preservation"
    root.mkdir()
    parked = db_path.parent / "recovery-preservation.parked"
    source_names_before = {p.name for p in library.iterdir()}
    target_before = target.read_bytes()
    donor_source_before = donor.read_bytes()
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)

    real_ensure = rp.ensure_directory_authority
    attacked = {"done": False}

    def swapping_bootstrap(path, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(root, parked)
            os.rename(library, root)
        return real_ensure(path, *args, **kwargs)

    monkeypatch.setattr(rp, "ensure_directory_authority", swapping_bootstrap)
    try:
        with pytest.raises(RecoveryPreservationError, match="filesystem object|Library|authority"):
            execute_preservation_stage(conn, plan)
    finally:
        if root.exists() and not library.exists():
            os.rename(root, library)
        if parked.exists():
            os.rename(parked, root)

    assert attacked["done"] is True
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_source_before == donor_before
    assert {p.name for p in library.iterdir()} == source_names_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0


def test_preservation_stage_creation_is_relative_to_bound_root_after_substitution(tmp_path: Path, monkeypatch) -> None:
    """Swap root after bootstrap validation; stage creation must not enter the source Library."""
    import os
    import ppa.recovery_preservation as rp
    from ppa.secure_write import BoundDirectory

    if not descriptor_bound_directory_mutation_available():
        pytest.skip("host-independent descriptor-relative stage creation regression")

    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    root = db_path.parent / "recovery-preservation"
    root.mkdir()
    from ppa.secure_write import bind_directory_authority
    from ppa.operational_authority import enroll_existing_directory
    _root_auth = bind_directory_authority(root)
    try:
        enroll_existing_directory(conn, "recovery_preservation", _root_auth)
    finally:
        _root_auth.close()
    parked = db_path.parent / "recovery-preservation.parked"
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    source_names_before = {p.name for p in library.iterdir()}
    target_before = target.read_bytes()
    donor_source_before = donor.read_bytes()

    real_create_child = BoundDirectory.create_directory_child
    attacked = {"done": False}

    def swapping_create_child(self, name, *args, **kwargs):
        if not attacked["done"] and self.path == root and name == plan.stage_id:
            attacked["done"] = True
            os.rename(root, parked)
            os.rename(library, root)
        return real_create_child(self, name, *args, **kwargs)

    monkeypatch.setattr(BoundDirectory, "create_directory_child", swapping_create_child)
    try:
        with pytest.raises(RecoveryPreservationError, match="created safely|authority|stage"):
            execute_preservation_stage(conn, plan)
    finally:
        if root.exists() and not library.exists():
            os.rename(root, library)
        if parked.exists():
            os.rename(parked, root)

    assert attacked["done"] is True
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_source_before == donor_before
    assert {p.name for p in library.iterdir()} == source_names_before
    assert not (library / plan.stage_id).exists()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0



def test_preservation_moved_library_subdirectory_cannot_become_operational_root(tmp_path: Path) -> None:
    """A historically observed source child may never become Phase-14 operational authority."""
    import os

    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(
        tmp_path, extra_source_dir="source-empty"
    )
    source_dir = library / "source-empty"
    assert list(source_dir.iterdir()) == []
    target_before = target.read_bytes()
    donor_source_before = donor.read_bytes()
    root = db_path.parent / "recovery-preservation"
    parked = db_path.parent / "recovery-preservation.parked"
    root.mkdir()
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)

    os.rename(root, parked)
    os.rename(source_dir, root)
    try:
        with pytest.raises(RecoveryPreservationError, match="source Library tree|source-tree"):
            execute_preservation_stage(conn, plan)
    finally:
        os.rename(root, source_dir)
        os.rename(parked, root)

    assert list(source_dir.iterdir()) == []
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_source_before == donor_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0


def test_preservation_postscan_unknown_source_child_cannot_replace_enrolled_root(tmp_path: Path) -> None:
    """A source directory created after the scan cannot inherit preservation-root ownership."""
    import os
    from ppa.operational_authority import enroll_existing_directory
    from ppa.secure_write import bind_directory_authority

    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    root = db_path.parent / "recovery-preservation"; root.mkdir()
    auth = bind_directory_authority(root)
    try:
        enroll_existing_directory(conn, "recovery_preservation", auth)
    finally:
        auth.close()
    parked = db_path.parent / "recovery-preservation.parked"; os.rename(root, parked)
    source_dir = library / "new-after-scan"; source_dir.mkdir(); user = source_dir / "user.txt"; user.write_text("SOURCE\n")
    before = user.read_bytes(); os.rename(source_dir, root)
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)
    try:
        with pytest.raises(RecoveryPreservationError, match="enrolled PPA operational object|replaced"):
            execute_preservation_stage(conn, plan)
    finally:
        os.rename(root, source_dir); os.rename(parked, root)
    assert user.read_bytes() == before
    assert {p.name for p in source_dir.iterdir()} == {"user.txt"}
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0
    assert target.read_bytes() == suspect and donor.read_bytes() == donor_before


def test_preservation_root_creation_provenance_rejects_postscan_source_inserted_after_absence(tmp_path: Path, monkeypatch) -> None:
    """14.1.15: only exact secure creation can bootstrap preservation-root ownership."""
    import os
    import ppa.recovery_preservation as rp

    conn, db_path, library, target, donor, _rows, _expected, donor_before, suspect, recorded = _recorded_case(tmp_path)
    root = db_path.parent / "recovery-preservation"
    assert not root.exists()

    new_source_dir = library / "new-after-scan"
    new_source_dir.mkdir()
    user = new_source_dir / "user.txt"
    user.write_bytes(b"POSTSCAN-SOURCE")
    before = user.read_bytes()
    target_before = target.read_bytes()
    donor_before_now = donor.read_bytes()
    plan = build_preservation_plan(conn, proposal_id=recorded.proposal_id)

    real_ensure = rp.ensure_directory_authority
    attacked = {"done": False}

    def insert_before_creator(path, *args, **kwargs):
        if not attacked["done"]:
            attacked["done"] = True
            os.rename(new_source_dir, root)
        return real_ensure(path, *args, **kwargs)

    monkeypatch.setattr(rp, "ensure_directory_authority", insert_before_creator)
    try:
        with pytest.raises(RecoveryPreservationError, match="not an enrolled PPA operational object|not created by this secure operation"):
            execute_preservation_stage(conn, plan)
    finally:
        if root.exists() and not new_source_dir.exists():
            os.rename(root, new_source_dir)

    assert attacked["done"] is True
    assert user.read_bytes() == before
    assert {p.name for p in new_source_dir.iterdir()} == {"user.txt"}
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_before_now == donor_before
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_directories WHERE purpose='recovery_preservation'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_preservation_stages").fetchone()[0] == 0


@pytest.mark.skipif(not descriptor_bound_directory_mutation_available(), reason="POSIX exact-destructive-authority regression")
def test_phase14117_stage_rmdir_never_deletes_substituted_source_directory(tmp_path: Path) -> None:
    """14.1.17: a historically recorded source directory substituted at stage name survives."""
    import os
    from ppa.secure_write import BoundDirectory

    conn, _db, library, _target, _donor, _rows, _expected, _donor_before, _suspect, recorded = _recorded_case(
        tmp_path, extra_source_dir="empty-user-folder"
    )
    result = execute_preservation_stage(conn, build_preservation_plan(conn, proposal_id=recorded.proposal_id))
    stage_dir = Path(result.manifest_path).parent
    source_dir = library / "empty-user-folder"
    known = conn.execute(
        "SELECT 1 FROM library_directory_identities WHERE canonical_path=? LIMIT 1",
        (str(source_dir.resolve()),),
    ).fetchone()
    assert known is not None

    parked = stage_dir.with_name(stage_dir.name + ".parked-14117")
    bound = BoundDirectory.open(stage_dir)
    try:
        os.rename(stage_dir, parked)
        os.rename(source_dir, stage_dir)
        assert bound.remove_self_if_still_named() is False
    finally:
        bound.close()

    assert stage_dir.is_dir()
    assert parked.is_dir()
    assert not source_dir.exists()  # same exact source directory now lives at stage_dir


def test_phase14117_create_directory_child_failure_never_rmdirs_replacement(tmp_path: Path, monkeypatch) -> None:
    """14.1.17: failed POSIX child creation retains a substituted replacement directory."""
    import os
    if os.name == "nt":
        pytest.skip("POSIX BoundDirectory failure-cleanup regression; Windows uses native handle authority")
    from ppa.secure_write import BoundDirectory, SecureWriteError

    parent_path = tmp_path / "operational"
    parent_path.mkdir()
    source = tmp_path / "source-directory"
    source.mkdir()
    sentinel = source / "user.txt"
    sentinel.write_bytes(b"SOURCE-DIRECTORY-MUST-SURVIVE")
    parked = parent_path / "child.parked"

    parent = BoundDirectory.open(parent_path)
    real_verify = BoundDirectory.verify_pathname
    attacked = {"done": False}

    def fail_after_substitution(self: BoundDirectory) -> None:
        if self.path == parent_path / "child" and not attacked["done"]:
            attacked["done"] = True
            os.rename(self.path, parked)
            os.rename(source, self.path)
            raise SecureWriteError("injected child validation failure after substitution")
        return real_verify(self)

    monkeypatch.setattr(BoundDirectory, "verify_pathname", fail_after_substitution)
    try:
        with pytest.raises(SecureWriteError, match="injected child validation failure"):
            parent.create_directory_child("child")
    finally:
        parent.close()

    assert attacked["done"] is True
    replacement = parent_path / "child"
    assert replacement.is_dir()
    assert (replacement / "user.txt").read_bytes() == b"SOURCE-DIRECTORY-MUST-SURVIVE"
    assert parked.is_dir()
