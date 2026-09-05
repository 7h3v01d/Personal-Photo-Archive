"""Phase 14.1 verified donor-materialization regressions."""
from __future__ import annotations

import hashlib
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
from ppa.secure_write import descriptor_bound_directory_mutation_available


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def _staged_case(
    tmp_path: Path,
    *,
    missing: bool = False,
    target_name: str = "target.jpg",
    donor_name: str = "donor.jpg",
):
    library = tmp_path / "library"
    target = library / target_name
    donor = library / donor_name
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
    inv = build_mismatch_investigation(conn, rows[target_name]["id"], thumbnail_cache_dir=tmp_path / "thumbs")
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
    phase13 = build_recovery_plan(conn, file_id=rows[target_name]["id"])
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
    assert row["donor_manifest_storage"] == "filesystem_file"
    assert row["donor_manifest_payload_json"] is None
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
    from ppa.safe_export import enroll_export_root
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


def test_donor_temp_hardlink_substitution_cannot_overwrite_suspect_target(tmp_path: Path, monkeypatch) -> None:
    import os
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    real_copy = rdm._copy_preserved_bytes

    def substitute_pending(source: Path, temporary):
        try:
            temporary.path.unlink()
            os.link(target, temporary.path)
        except (OSError, NotImplementedError):
            pytest.skip("hard-link substitution unavailable in this environment")
        return real_copy(source, temporary)

    monkeypatch.setattr(rdm, "_copy_preserved_bytes", substitute_pending)
    with pytest.raises(RecoveryDonorMaterializationError, match="temporary identity"):
        execute_donor_materialization(conn, plan)
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    with Image.open(target) as image:
        image.verify()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0


def test_keyboard_interrupt_after_install_respects_platform_cleanup_authority(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)

    def interrupt_manifest(_path: Path, _payload: dict, *_args) -> str:
        raise KeyboardInterrupt()

    real_manifest = rdm._write_json_manifest
    monkeypatch.setattr(rdm, "_write_json_manifest", interrupt_manifest)
    with pytest.raises(KeyboardInterrupt):
        execute_donor_materialization(conn, plan)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before

    destination = Path(plan.donor_materialization_path)
    manifest = Path(plan.donor_manifest_path)
    # 14.1.17 extends the non-destructive rule to POSIX child cleanup.  The
    # completed donor is retained and recovered forward on every platform.
    assert destination.is_file()
    assert not manifest.exists()
    monkeypatch.setattr(rdm, "_write_json_manifest", real_manifest)
    result = rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert result["state"] == "orphan_artifact_adopted"
    assert result["manifest_storage"] == "catalogue_embedded"
    row = conn.execute(
        "SELECT * FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert row is not None
    assert row["donor_manifest_storage"] == "catalogue_embedded"
    assert not Path(row["donor_manifest_path"]).exists()


def test_orphan_reconciliation_unblocks_crash_stranded_stage(tmp_path: Path) -> None:
    from ppa.recovery_donor_materialization import reconcile_donor_materialization_orphans

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    initial = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(initial.donor_materialization_path)
    manifest = Path(initial.donor_manifest_path)
    destination.write_bytes(donor_before)
    manifest.write_text('{"orphan": true}\n', encoding="utf-8")
    pending = destination.parent / "expected-donor.crash.pending"
    pending.write_bytes(b"partial-operational-copy")

    with pytest.raises(RecoveryDonorMaterializationError, match="orphan|uncheckpointed"):
        build_donor_materialization_plan(conn, stage_id=stage.stage_id)

    # 14.1.17: temporary/ambiguous debris is never deleted automatically on
    # POSIX either.  It remains for explicit manual intervention.
    with pytest.raises(RecoveryDonorMaterializationError, match="manual intervention"):
        reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert destination.is_file()
    assert manifest.is_file()
    assert pending.is_file()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_recorded_donor_materialization_cannot_be_deleted_or_cascade_erased(tmp_path: Path) -> None:
    import sqlite3
    conn, _db, _library, _target, _donor, rows, *_rest, stage = _staged_case(tmp_path)
    result = execute_donor_materialization(conn, build_donor_materialization_plan(conn, stage_id=stage.stage_id))
    with pytest.raises(sqlite3.IntegrityError, match="append-only|cannot be deleted"):
        conn.execute("DELETE FROM archive_recovery_donor_materializations WHERE materialization_id=?", (result.materialization_id,))
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations WHERE materialization_id=?", (result.materialization_id,)).fetchone()[0] == 1

    # Parent ON DELETE CASCADE is not allowed to erase immutable recovery history.
    with pytest.raises(sqlite3.IntegrityError, match="append-only|cannot be deleted"):
        conn.execute("DELETE FROM files WHERE id=?", (rows["target.jpg"]["id"],))
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations WHERE materialization_id=?", (result.materialization_id,)).fetchone()[0] == 1


@pytest.mark.skipif(not descriptor_bound_directory_mutation_available(), reason="POSIX bound-stage concurrency regression")
def test_orphan_reconciliation_serializes_and_retains_ambiguous_debris(tmp_path: Path, monkeypatch) -> None:
    """14.1.17: writer serialization remains, but ambiguous debris is retained."""
    import threading
    import time
    import ppa.recovery_donor_materialization as rdm

    conn, db_path, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(plan.donor_materialization_path)
    manifest = Path(plan.donor_manifest_path)
    # Simulate crash debris that reconciliation is expected to remove.
    destination.write_bytes(b"orphan-donor")
    manifest.write_text('{"orphan": true}\n', encoding="utf-8")
    conn.close()

    entered_reconcile = threading.Event()
    release_reconcile = threading.Event()
    materializer_started = threading.Event()
    materializer_done = threading.Event()
    errors: list[BaseException] = []
    results: dict[str, object] = {}
    real_verify = rdm._verify_committed_stage_evidence
    paused = {"done": False}

    def pausing_verify(row, stage_dir):
        result = real_verify(row, stage_dir)
        if threading.current_thread().name == "reconciler" and not paused["done"]:
            paused["done"] = True
            entered_reconcile.set()
            assert release_reconcile.wait(5)
        return result

    monkeypatch.setattr(rdm, "_verify_committed_stage_evidence", pausing_verify)

    def run_reconcile() -> None:
        c = connect(db_path)
        try:
            results["reconcile"] = rdm.reconcile_donor_materialization_orphans(c, stage_id=stage.stage_id)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            c.close()

    def run_materializer() -> None:
        c = connect(db_path)
        try:
            materializer_started.set()
            results["materialize"] = rdm.execute_donor_materialization(c, plan)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            materializer_done.set()
            c.close()

    t1 = threading.Thread(target=run_reconcile, name="reconciler")
    t1.start()
    assert entered_reconcile.wait(5)
    t2 = threading.Thread(target=run_materializer, name="materializer")
    t2.start()
    assert materializer_started.wait(5)
    # BEGIN IMMEDIATE in the materializer must be blocked while reconciliation
    # owns the writer-authority transaction.
    time.sleep(0.15)
    assert not materializer_done.is_set()
    release_reconcile.set()
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive() and not t2.is_alive()
    # Reconciliation owns the writer transaction first, then fails closed on the
    # deliberately invalid orphan.  The materializer subsequently observes that
    # the uncheckpointed debris still exists and also refuses to proceed.
    assert len(errors) == 2
    assert any("manual intervention" in str(exc) for exc in errors)
    assert any("orphan reconciliation" in str(exc) for exc in errors)

    check = connect(db_path)
    try:
        row = check.execute(
            "SELECT * FROM archive_recovery_donor_materializations WHERE stage_id=?",
            (stage.stage_id,),
        ).fetchone()
        assert row is None
    finally:
        check.close()
    assert destination.is_file()
    assert manifest.is_file()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_interrupt_raised_immediately_after_sqlite_commit_preserves_donor_evidence(tmp_path: Path) -> None:
    """Cover the ambiguous commit→Python-flag interval explicitly."""
    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)

    class InterruptAfterCommit:
        def __init__(self, inner):
            self.inner = inner
            self.raised = False

        @property
        def in_transaction(self):
            return self.inner.in_transaction

        def execute(self, *args, **kwargs):
            return self.inner.execute(*args, **kwargs)

        def commit(self):
            self.inner.commit()
            if not self.raised:
                self.raised = True
                raise KeyboardInterrupt()

        def rollback(self):
            return self.inner.rollback()

    proxy = InterruptAfterCommit(conn)
    with pytest.raises(KeyboardInterrupt):
        execute_donor_materialization(proxy, plan)

    row = conn.execute(
        "SELECT * FROM archive_recovery_donor_materializations WHERE materialization_id=?",
        (plan.materialization_id,),
    ).fetchone()
    assert row is not None
    assert Path(row["donor_materialization_path"]).read_bytes() == donor_before
    assert Path(row["donor_manifest_path"]).is_file()
    assert row["donor_manifest_storage"] == "filesystem_file"
    assert row["donor_manifest_payload_json"] is None
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_donor_rollback_cleanup_retains_posix_debris(tmp_path: Path, monkeypatch) -> None:
    """14.1.17: rollback never calls POSIX child-name unlink."""
    import ppa.recovery_donor_materialization as rdm
    from ppa.secure_write import BoundDirectory

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(
        tmp_path, donor_name="expected-donor.jpg"
    )
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)

    def fail_manifest(_path: Path, _payload: dict, *_args) -> str:
        raise RecoveryDonorMaterializationError("injected manifest failure")

    def forbidden_unlink(self: BoundDirectory, name: str) -> bool:
        raise AssertionError(f"POSIX donor rollback must not unlink child by name: {name}")

    monkeypatch.setattr(rdm, "_write_json_manifest", fail_manifest)
    monkeypatch.setattr(BoundDirectory, "unlink_child", forbidden_unlink)

    with pytest.raises(RecoveryDonorMaterializationError, match="injected manifest failure"):
        execute_donor_materialization(conn, plan)

    assert Path(plan.donor_materialization_path).is_file()
    assert donor.exists() and donor.read_bytes() == donor_before
    assert target.read_bytes() == suspect
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0



def test_orphan_reconciliation_invalid_debris_is_retained_without_removed_event(tmp_path: Path) -> None:
    """14.1.17: invalid orphan debris is retained and never reported removed."""
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(
        tmp_path, donor_name="expected-donor.jpg"
    )
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(plan.donor_materialization_path)
    manifest = Path(plan.donor_manifest_path)
    destination.write_bytes(b"orphan-operational-donor")
    manifest.write_text('{"orphan": true}\n', encoding="utf-8")

    with pytest.raises(RecoveryDonorMaterializationError, match="manual intervention"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)

    assert destination.is_file()
    assert manifest.is_file()
    assert donor.exists() and donor.read_bytes() == donor_before
    assert target.read_bytes() == suspect
    event = conn.execute(
        "SELECT 1 FROM integrity_events WHERE event_type='archive_recovery_donor_orphan_reconciled' LIMIT 1"
    ).fetchone()
    assert event is None



def test_windows_style_orphan_forward_path_adopts_verified_final_artifact(tmp_path: Path, monkeypatch) -> None:
    """When destructive cleanup is unavailable, a proven final orphan moves forward."""
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    real_manifest = rdm._write_json_manifest

    # Simulate the Windows policy: no descriptor-bound directory deletion is
    # available, then interrupt after the final donor has been installed but
    # before a manifest/checkpoint can be completed.
    monkeypatch.setattr(rdm, "_try_bind_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        rdm,
        "_write_json_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RecoveryDonorMaterializationError("injected pre-manifest interruption")
        ),
    )
    with pytest.raises(RecoveryDonorMaterializationError, match="pre-manifest"):
        execute_donor_materialization(conn, plan)

    destination = Path(plan.donor_materialization_path)
    assert destination.is_file()
    assert destination.read_bytes() == donor_before
    assert not Path(plan.donor_manifest_path).exists()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0

    monkeypatch.setattr(rdm, "_write_json_manifest", real_manifest)
    result = rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert result["state"] == "orphan_artifact_adopted"
    assert result["removed"] == []
    row = conn.execute(
        "SELECT * FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert row is not None
    assert Path(row["donor_materialization_path"]).read_bytes() == donor_before
    assert not Path(row["donor_manifest_path"]).exists()
    assert row["donor_manifest_storage"] == "catalogue_embedded"
    assert row["donor_manifest_payload_json"]
    payload_raw = row["donor_manifest_payload_json"].encode("utf-8")
    assert hashlib.sha256(payload_raw).hexdigest() == row["donor_manifest_sha256"]
    payload = json.loads(row["donor_manifest_payload_json"])
    assert payload["schema"] == "ppa-recovery-donor-manifest/1"
    assert payload["materialization_id"] == row["materialization_id"]
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_windows_style_orphan_forward_path_refuses_invalid_artifact(tmp_path: Path, monkeypatch) -> None:
    """Unsafe/invalid Windows debris is retained for manual intervention."""
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(plan.donor_materialization_path)
    destination.write_bytes(b"not the expected image")
    before = destination.read_bytes()
    monkeypatch.setattr(rdm, "_try_bind_stage", lambda *_args, **_kwargs: None)

    with pytest.raises(RecoveryDonorMaterializationError, match="expected|physical|image|manual"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert destination.read_bytes() == before
    assert destination.exists()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_windows_style_orphan_forward_path_refuses_temp_debris(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    stage_dir = Path(stage.manifest_path).parent
    pending = stage_dir / "expected-donor.stranded.pending"
    pending.write_bytes(b"stranded")
    monkeypatch.setattr(rdm, "_try_bind_stage", lambda *_args, **_kwargs: None)

    with pytest.raises(RecoveryDonorMaterializationError, match="manual intervention"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert pending.read_bytes() == b"stranded"
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_windows_orphan_adoption_never_writes_manifest_through_replaced_real_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """A real-directory substitution at the old manifest-write boundary cannot modify the Library."""
    import os
    import shutil
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(plan.donor_materialization_path)
    shutil.copyfile(donor, destination)
    source_sidecar = library / "donor-materialization.json"
    source_sidecar.write_bytes(b"USER-SOURCE-DATA")
    before = source_sidecar.read_bytes()
    stage_dir = Path(stage.manifest_path).parent
    parked_stage = stage_dir.with_name(stage_dir.name + ".parked-adoption")
    parked_library = library.with_name(library.name + ".parked-adoption")

    monkeypatch.setattr(rdm, "_try_bind_stage", lambda *_args, **_kwargs: None)
    real_canonical = rdm._canonical_manifest_json
    swapped = {"done": False}

    def swap_at_manifest_boundary(payload):
        result = real_canonical(payload)
        os.rename(stage_dir, parked_stage)
        os.rename(library, parked_library)
        os.rename(parked_library, stage_dir)
        swapped["done"] = True
        return result

    monkeypatch.setattr(rdm, "_canonical_manifest_json", swap_at_manifest_boundary)
    try:
        with pytest.raises(RecoveryDonorMaterializationError, match="stage changed"):
            rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
        assert swapped["done"]
        assert (stage_dir / "donor-materialization.json").read_bytes() == before
        assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0
    finally:
        # stage_dir currently names the moved source Library. Restore both real objects.
        if stage_dir.exists():
            os.rename(stage_dir, parked_library)
        if parked_stage.exists():
            os.rename(parked_stage, stage_dir)
        if parked_library.exists():
            os.rename(parked_library, library)

    assert source_sidecar.read_bytes() == before
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


@pytest.mark.skipif(__import__("os").name != "nt", reason="native NTFS/Windows recovery boundary")
def test_windows_native_interrupted_donor_can_be_adopted(tmp_path: Path, monkeypatch) -> None:
    """Native Windows gate: no deletion authority is needed to recover a final orphan."""
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    real_manifest = rdm._write_json_manifest
    monkeypatch.setattr(
        rdm,
        "_write_json_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RecoveryDonorMaterializationError("native Windows interruption")
        ),
    )
    with pytest.raises(RecoveryDonorMaterializationError, match="native Windows"):
        execute_donor_materialization(conn, plan)
    assert Path(plan.donor_materialization_path).is_file()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0

    monkeypatch.setattr(rdm, "_write_json_manifest", real_manifest)
    result = rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert result["state"] == "orphan_artifact_adopted"
    assert result["manifest_storage"] == "catalogue_embedded"
    row = conn.execute(
        "SELECT * FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert row["donor_manifest_storage"] == "catalogue_embedded"
    assert row["donor_manifest_payload_json"]
    assert not Path(row["donor_manifest_path"]).exists()
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_windows_style_orphan_forward_path_refuses_hardlink_alias(tmp_path: Path, monkeypatch) -> None:
    import os
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(plan.donor_materialization_path)
    try:
        os.link(donor, destination)
    except OSError:
        pytest.skip("hard links unavailable in this environment")
    monkeypatch.setattr(rdm, "_try_bind_stage", lambda *_args, **_kwargs: None)

    with pytest.raises(RecoveryDonorMaterializationError, match="hard links|aliases"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert donor.read_bytes() == donor_before
    assert target.read_bytes() == suspect
    assert destination.exists()
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0


def test_windows_style_orphan_forward_path_adopts_existing_valid_manifest(tmp_path: Path, monkeypatch) -> None:
    import shutil
    import ppa.recovery_donor_materialization as rdm

    conn, _db, _library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = rdm._build_donor_materialization_plan(
        conn,
        stage_id=stage.stage_id,
        materialization_id=None,
        allow_uncheckpointed_artifacts=True,
    )
    destination = Path(plan.donor_materialization_path)
    shutil.copyfile(donor, destination)
    donor_obs = rdm.observe_stable_image(donor, expected_sha256=plan.expected_sha256)
    materialized_at = rdm._now()
    payload = rdm._orphan_manifest_payload(
        plan,
        donor_obs,
        copied_size=len(donor_before),
        materialized_at=materialized_at,
    )
    rdm._write_json_manifest(Path(plan.donor_manifest_path), payload)
    monkeypatch.setattr(rdm, "_try_bind_stage", lambda *_args, **_kwargs: None)

    result = rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert result["state"] == "orphan_artifact_adopted"
    assert result["materialization_id"] == plan.materialization_id
    row = conn.execute(
        "SELECT * FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert row["materialization_id"] == plan.materialization_id
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


@pytest.mark.skipif(__import__("os").name != "nt", reason="native NTFS/Windows junction recovery boundary")
def test_windows_native_stage_junction_substitution_is_rejected(tmp_path: Path) -> None:
    import os
    import subprocess

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    stage_dir = Path(stage.manifest_path).parent
    parked = stage_dir.with_name(stage_dir.name + ".parked")
    os.rename(stage_dir, parked)
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(stage_dir), str(library)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        os.rename(parked, stage_dir)
        pytest.skip(f"could not create NTFS junction: {proc.stderr or proc.stdout}")
    try:
        with pytest.raises(RecoveryDonorMaterializationError, match="reparse|unsafe|junction"):
            build_donor_materialization_plan(conn, stage_id=stage.stage_id)
        assert donor.read_bytes() == donor_before
        assert target.read_bytes() == suspect
    finally:
        subprocess.run(["cmd", "/c", "rmdir", str(stage_dir)], capture_output=True)
        os.rename(parked, stage_dir)


def test_normal_donor_manifest_parent_real_directory_substitution_cannot_touch_library(tmp_path: Path, monkeypatch) -> None:
    """Normal Phase-14.1 manifest creation cannot re-authorise a substituted real directory."""
    import os
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    source_manifest = library / "donor-materialization.json"
    source_manifest.write_bytes(b"USER-SOURCE-DATA")
    target_before = target.read_bytes()
    donor_source_before = donor.read_bytes()
    source_manifest_before = source_manifest.read_bytes()
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    stage_path = Path(plan.stage_dir)
    parked = stage_path.with_name(stage_path.name + ".parked")
    real_manifest = rdm._write_json_manifest
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

    monkeypatch.setattr(rdm, "_write_json_manifest", swapping_manifest)
    with pytest.raises(RecoveryDonorMaterializationError, match="manifest|temporary|identity|parent"):
        execute_donor_materialization(conn, plan)

    assert attacked["done"] is True
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_source_before == donor_before
    assert source_manifest.read_bytes() == source_manifest_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0


def test_donor_copy_parent_real_directory_substitution_cannot_touch_library(tmp_path: Path, monkeypatch) -> None:
    """The first normal Phase-14.1 donor temp write must bind the committed stage object."""
    import os
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    collision = library / "expected-donor.attack.pending"
    collision.write_bytes(b"USER-DATA")
    collision_before = collision.read_bytes()
    target_before = target.read_bytes()
    donor_source_before = donor.read_bytes()
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    stage_path = Path(plan.stage_dir)
    parked = stage_path.with_name(stage_path.name + ".parked")
    real_create = rdm.BoundTemporaryFile.create
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

    monkeypatch.setattr(rdm.BoundTemporaryFile, "create", staticmethod(swapping_create))
    with pytest.raises(RecoveryDonorMaterializationError, match="temporary|identity|parent|expected|path"):
        execute_donor_materialization(conn, plan)

    assert attacked["done"] is True
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_source_before == donor_before
    assert collision.read_bytes() == collision_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0


@pytest.mark.skipif(not descriptor_bound_directory_mutation_available(), reason="POSIX exact-destructive-authority regression")
def test_phase14117_cleanup_owned_never_deletes_substituted_catalogued_source(tmp_path: Path) -> None:
    """14.1.17: production rollback cleanup leaves a substituted source object intact."""
    import os
    import ppa.recovery_donor_materialization as rdm
    from ppa.secure_write import BoundDirectory

    conn, _db, library, _target, _donor, _rows, _donor_before, _suspect, _proposal, stage = _staged_case(tmp_path)
    source = library / "catalogued-extra.jpg"
    _img(source, "green")
    scan_library(conn, library)
    source_row = conn.execute("SELECT id FROM files WHERE filename='catalogued-extra.jpg'").fetchone()
    assert source_row is not None
    source_bytes = source.read_bytes()

    stage_dir = Path(stage.manifest_path).parent
    owned = stage_dir / "expected-donor.jpg"
    parked = stage_dir / "parked-owned.bin"
    owned.write_bytes(b"PPA-OWNED-ROLLBACK-CHILD")

    bound = BoundDirectory.open(stage_dir)
    try:
        # Reproduce the review's post-check namespace shape: the PPA child has
        # been moved aside and a catalogued source object now occupies its name.
        os.rename(owned, parked)
        os.rename(source, owned)
        rdm._cleanup_owned([owned], bound)
    finally:
        bound.close()

    assert not source.exists()
    assert owned.read_bytes() == source_bytes
    assert parked.read_bytes() == b"PPA-OWNED-ROLLBACK-CHILD"
    # The catalogue still records the source file; crucially its bytes survive.
    assert conn.execute("SELECT 1 FROM files WHERE id=?", (source_row["id"],)).fetchone() is not None


@pytest.mark.skipif(not descriptor_bound_directory_mutation_available(), reason="POSIX exact-destructive-authority regression")
def test_phase14117_orphan_reconciliation_retains_substituted_source_temp(tmp_path: Path) -> None:
    """14.1.17: donor orphan reconciliation never unlinks a source object in a temp slot."""
    import os
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    source = library / "catalogued-orphan-source.jpg"
    _img(source, "yellow")
    scan_library(conn, library)
    assert conn.execute("SELECT 1 FROM files WHERE filename='catalogued-orphan-source.jpg'").fetchone() is not None
    source_bytes = source.read_bytes()

    stage_dir = Path(stage.manifest_path).parent
    pending = stage_dir / "expected-donor.attack.pending"
    os.rename(source, pending)

    with pytest.raises(RecoveryDonorMaterializationError, match="manual intervention"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)

    assert pending.read_bytes() == source_bytes
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    event = conn.execute(
        "SELECT 1 FROM integrity_events WHERE event_type='archive_recovery_donor_orphan_reconciled' LIMIT 1"
    ).fetchone()
    assert event is None


def test_phase141172_orphan_adoption_rejects_current_catalogued_source_identity(tmp_path: Path) -> None:
    """14.1.17.2: exact source-object identity cannot be adopted as donor evidence."""
    import os
    import stat
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    source = library / "catalogued-duplicate.jpg"
    source.write_bytes(donor_before)
    source.chmod(0o600)
    scan_library(conn, library)
    source_row = conn.execute(
        "SELECT id,fs_device_id,fs_object_id FROM files WHERE filename=?",
        (source.name,),
    ).fetchone()
    assert source_row is not None
    before_stat = source.stat()
    before_mode = stat.S_IMODE(before_stat.st_mode)
    assert (source_row["fs_device_id"], source_row["fs_object_id"]) == (
        str(before_stat.st_dev), str(before_stat.st_ino)
    )

    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(plan.donor_materialization_path)
    os.rename(source, destination)

    with pytest.raises(RecoveryDonorMaterializationError, match="source-library evidence|source.*authority|manual intervention"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)

    assert not source.exists()
    assert destination.read_bytes() == donor_before
    assert stat.S_IMODE(destination.stat().st_mode) == before_mode
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT 1 FROM integrity_events WHERE event_type='archive_recovery_donor_orphan_adopted' LIMIT 1"
    ).fetchone() is None


def test_phase141172_orphan_adoption_rejects_historical_catalogued_source_identity(tmp_path: Path) -> None:
    """14.1.17.2: source authority survives later observation of a replacement inode."""
    import os
    import stat
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    source = library / "historical-source.jpg"
    source.write_bytes(donor_before)
    source.chmod(0o600)
    scan_library(conn, library)
    source_row = conn.execute(
        "SELECT id,fs_device_id,fs_object_id FROM files WHERE filename=?",
        (source.name,),
    ).fetchone()
    assert source_row is not None
    old_identity = (source_row["fs_device_id"], source_row["fs_object_id"])
    old_mode = stat.S_IMODE(source.stat().st_mode)

    parked = tmp_path / "parked-historical-source.jpg"
    os.rename(source, parked)
    # A different filesystem object now occupies the same registered source path.
    source.write_bytes(donor_before)
    scan_library(conn, library)
    current = conn.execute(
        "SELECT fs_device_id,fs_object_id FROM files WHERE id=?",
        (source_row["id"],),
    ).fetchone()
    assert (current["fs_device_id"], current["fs_object_id"]) != old_identity
    assert conn.execute(
        "SELECT 1 FROM file_storage_identity_history WHERE file_id=? AND device_id=? AND object_id=? LIMIT 1",
        (source_row["id"], old_identity[0], old_identity[1]),
    ).fetchone() is not None

    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(plan.donor_materialization_path)
    os.rename(parked, destination)

    with pytest.raises(RecoveryDonorMaterializationError, match="source-library evidence|source.*authority|manual intervention"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)

    assert destination.read_bytes() == donor_before
    assert stat.S_IMODE(destination.stat().st_mode) == old_mode
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT 1 FROM integrity_events WHERE event_type='archive_recovery_donor_orphan_adopted' LIMIT 1"
    ).fetchone() is None


def test_phase141172_existing_orphan_manifest_rejects_source_authority_identity(tmp_path: Path) -> None:
    """14.1.17.2: a filesystem manifest object carrying source authority is never adopted."""
    import shutil
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    extra = library / "manifest-authority-anchor.jpg"
    _img(extra, "green")
    scan_library(conn, library)
    extra_row = conn.execute("SELECT id FROM files WHERE filename=?", (extra.name,)).fetchone()
    assert extra_row is not None

    plan = rdm._build_donor_materialization_plan(
        conn, stage_id=stage.stage_id, materialization_id=None, allow_uncheckpointed_artifacts=True
    )
    destination = Path(plan.donor_materialization_path)
    manifest = Path(plan.donor_manifest_path)
    shutil.copyfile(donor, destination)
    donor_obs = rdm.observe_stable_image(donor, expected_sha256=plan.expected_sha256)
    payload = rdm._orphan_manifest_payload(
        plan, donor_obs, copied_size=len(donor_before), materialized_at=rdm._now()
    )
    rdm._write_json_manifest(manifest, payload)
    mst = manifest.stat()
    # Model a still-registered source File whose exact object identity is this
    # valid JSON object.  The adoption boundary must consult source authority
    # before accepting the manifest as operational evidence.
    conn.execute(
        "UPDATE files SET fs_device_id=?,fs_object_id=? WHERE id=?",
        (str(mst.st_dev), str(mst.st_ino), extra_row["id"]),
    )
    conn.commit()
    before = manifest.read_bytes()

    with pytest.raises(RecoveryDonorMaterializationError, match="source-library evidence|source.*authority|manual intervention"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)

    assert manifest.read_bytes() == before
    assert destination.read_bytes() == donor_before
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_donor_materializations").fetchone()[0] == 0


def test_phase141174_orphan_late_hardlink_blocks_adoption_and_preserves_mode(tmp_path: Path, monkeypatch) -> None:
    """14.1.17.4: a hard link arriving after initial orphan checks invalidates adoption."""
    import os
    import stat
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    real_manifest = rdm._write_json_manifest
    monkeypatch.setattr(rdm, "_try_bind_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        rdm,
        "_write_json_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RecoveryDonorMaterializationError("injected pre-manifest interruption")
        ),
    )
    with pytest.raises(RecoveryDonorMaterializationError, match="pre-manifest"):
        execute_donor_materialization(conn, plan)

    destination = Path(plan.donor_materialization_path)
    alias = library / "late-source-hardlink.jpg"
    before_mode = stat.S_IMODE(destination.stat().st_mode)
    real_observe = rdm.observe_stable_image
    attacked = {"done": False}

    def observe_and_link(path, *args, **kwargs):
        result = real_observe(path, *args, **kwargs)
        if not attacked["done"] and Path(path) == destination:
            os.link(destination, alias)
            attacked["done"] = True
        return result

    monkeypatch.setattr(rdm, "_write_json_manifest", real_manifest)
    monkeypatch.setattr(rdm, "observe_stable_image", observe_and_link)
    with pytest.raises(RecoveryDonorMaterializationError, match="hard-link|single-link|alias"):
        rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)

    assert attacked["done"]
    assert destination.exists() and alias.exists()
    assert destination.stat().st_nlink == 2
    assert alias.stat().st_nlink == 2
    assert stat.S_IMODE(destination.stat().st_mode) == before_mode
    assert stat.S_IMODE(alias.stat().st_mode) == before_mode
    assert destination.read_bytes() == donor_before
    assert alias.read_bytes() == donor_before
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT 1 FROM integrity_events WHERE event_type='archive_recovery_donor_orphan_adopted' LIMIT 1"
    ).fetchone() is None
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before


def test_phase141174_normal_donor_late_hardlink_blocks_checkpoint_without_chmod(tmp_path: Path, monkeypatch) -> None:
    """14.1.17.4: normal donor finalisation re-attests single-link topology."""
    import os
    import stat
    import ppa.recovery_donor_materialization as rdm

    conn, _db, library, target, donor, _rows, donor_before, suspect, _proposal, stage = _staged_case(tmp_path)
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    destination = Path(plan.donor_materialization_path)
    manifest = Path(plan.donor_manifest_path)
    alias = library / "late-materialized-hardlink.jpg"
    real_observe = rdm.observe_stable_image
    attacked = {"done": False, "mode": None}

    def observe_and_link(path, *args, **kwargs):
        result = real_observe(path, *args, **kwargs)
        if (
            not attacked["done"]
            and destination.exists()
            and manifest.exists()
            and Path(path) == Path(plan.donor_path)
        ):
            os.link(destination, alias)
            attacked["done"] = True
            attacked["mode"] = stat.S_IMODE(alias.stat().st_mode)
        return result

    monkeypatch.setattr(rdm, "observe_stable_image", observe_and_link)
    with pytest.raises(RecoveryDonorMaterializationError, match="hard-link|single-link|alias"):
        execute_donor_materialization(conn, plan)

    assert attacked["done"] and alias.exists()
    assert alias.read_bytes() == donor_before
    assert stat.S_IMODE(alias.stat().st_mode) == attacked["mode"]
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT 1 FROM integrity_events WHERE event_type='archive_recovery_donor_materialized' LIMIT 1"
    ).fetchone() is None
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_before
