"""Phase 14.3 target-replacement execution regressions."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import uuid

import pytest
from PIL import Image

from ppa.db import connect, current_schema_version
from ppa.integrity import verify_library
from ppa.mismatch_investigation import build_mismatch_investigation
from ppa.mismatch_resolution import ACTION_RETAIN_EXPECTED, execute_mismatch_resolution, plan_mismatch_resolution
from ppa.recovery_planning import build_recovery_plan, record_recovery_plan_proposal
from ppa.recovery_preservation import build_preservation_plan, execute_preservation_stage
from ppa.recovery_donor_materialization import build_donor_materialization_plan, execute_donor_materialization
from ppa.recovery_target_readiness import build_target_replacement_readiness, record_target_replacement_readiness
from ppa.recovery_target_execution import (
    AUTHORIZATION_STATE,
    EXECUTION_PLAN_SCHEMA,
    RESULT_ABORTED,
    RESULT_PLACED,
    RESULT_RESTORED,
    RecoveryTargetExecutionError,
    build_target_replacement_execution_plan,
    execute_target_replacement,
    inspect_recovery_execution_status,
)
from ppa.scanner import scan_library


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def _open_windows_shared_write_fd(path: Path) -> int:
    """Open an independent writable Windows handle that shares delete.

    The handle is acquired before PPA parks the target so it continues to name
    the exact same filesystem object across the handle-relative rename.  This
    lets the native rollback regression perform a real concurrent in-place
    mutation instead of relying on a pathname reopen that Windows may deny while
    PPA's exact-object handles are live.
    """
    if os.name != "nt":
        raise RuntimeError("native Windows shared-write handle is unavailable")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    generic_write = 0x40000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        generic_read | generic_write,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    value = int(handle) if handle is not None else 0
    if not value or value == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(value, os.O_RDWR | getattr(os, "O_BINARY", 0))
        value = 0  # CRT fd owns the native handle now.
        return fd
    finally:
        if value:
            close_handle(wintypes.HANDLE(value))


def _case(tmp_path: Path, *, missing: bool = True, nested: bool = True):
    library = tmp_path / "library"
    base = library / "album" if nested else library
    target = base / "target.jpg"
    donor = base / "donor.jpg"
    _img(target, "red")
    _img(donor, "red")
    expected_bytes = donor.read_bytes()
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    rows = {r["filename"]: r for r in conn.execute("SELECT * FROM files")}

    _img(target, "blue")
    suspect_bytes = target.read_bytes()
    assert verify_library(conn).mismatches == 1
    inv = build_mismatch_investigation(
        conn, rows["target.jpg"]["id"], thumbnail_cache_dir=tmp_path / "thumbs"
    )
    execute_mismatch_resolution(
        conn,
        plan_mismatch_resolution(
            conn,
            file_id=inv.file_id,
            action=ACTION_RETAIN_EXPECTED,
            reviewed_expected_revision_id=inv.expected_revision_id,
            reviewed_expected_sha256=inv.expected_sha256,
            reviewed_current_state=inv.current_state,
            reviewed_current_sha256=inv.current_observed_sha256,
            reviewed_observation_id=inv.verify_observation_id,
        ),
    )
    if missing:
        target.unlink()
    proposal = record_recovery_plan_proposal(conn, build_recovery_plan(conn, file_id=rows["target.jpg"]["id"]))
    stage = execute_preservation_stage(conn, build_preservation_plan(conn, proposal_id=proposal.proposal_id))
    materialized = execute_donor_materialization(
        conn, build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    )
    readiness = build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    recorded = record_target_replacement_readiness(conn, readiness)
    return conn, library, target, donor, expected_bytes, suspect_bytes, rows, materialized, recorded


def _plan(conn, recorded, execution_id: str | None = None):
    return build_target_replacement_execution_plan(
        conn, readiness_id=recorded.readiness_id, execution_id=execution_id
    )


def test_phase143_schema_v41_and_execution_ledgers_have_authority_constraints(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")
    assert current_schema_version(conn) == 41
    sql = "\n".join(
        r["sql"] or "" for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name IN "
            "('archive_recovery_target_execution_attempts','archive_recovery_target_execution_results')"
        )
    )
    assert "target_replacement_authorized=1" in sql
    assert "recovery_execution_authorized=1" in sql
    assert "confirmed_one_attempt" in sql
    assert "expected_target_placed_verified" in sql


def test_phase143_preview_is_read_only_zero_authority_and_confirmation_is_plan_bound(tmp_path: Path) -> None:
    conn, _library, target, donor, expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=True)
    assert not target.exists()
    donor_before = donor.read_bytes()
    plan = _plan(conn, recorded)
    assert plan.schema == EXECUTION_PLAN_SCHEMA
    assert plan.target_replacement_authorized is False
    assert plan.recovery_execution_authorized is False
    assert plan.confirmation_phrase.startswith(f"EXECUTE PPA RECOVERY {plan.execution_id} ")
    assert plan.execution_plan_fingerprint[:16] in plan.confirmation_phrase
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_execution_attempts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_execution_results").fetchone()[0] == 0
    assert not target.exists()
    assert donor.read_bytes() == donor_before == expected


def test_phase143_wrong_confirmation_creates_no_authority_or_source_change(tmp_path: Path) -> None:
    conn, _library, target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)
    with pytest.raises(RecoveryTargetExecutionError, match="confirmation"):
        execute_target_replacement(conn, plan, confirmation="NO")
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_execution_attempts").fetchone()[0] == 0
    assert not target.exists()


def test_phase143_missing_target_restore_places_expected_but_verify_owns_health(tmp_path: Path) -> None:
    conn, _library, target, _donor, expected, _suspect, rows, _mat, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)
    result = execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase, note="native test")
    assert result.result_state == RESULT_PLACED
    assert result.verify_reconciliation_required is True
    assert target.read_bytes() == expected
    row = conn.execute("SELECT health_status FROM files WHERE id=?", (rows["target.jpg"]["id"],)).fetchone()
    assert row["health_status"] == "hash_mismatch"
    summary = verify_library(conn)
    assert summary.verified_ok >= 1
    row = conn.execute("SELECT health_status FROM files WHERE id=?", (rows["target.jpg"]["id"],)).fetchone()
    assert row["health_status"] == "ok"



def test_phase1433_missing_restore_internal_post_acquisition_failure_stays_unresolved(
    tmp_path: Path, monkeypatch
) -> None:
    """An install-internal failure after target acquisition must stay unresolved.

    POSIX reproduces the reviewer's exact boundary: RENAME_NOREPLACE succeeds
    and the following parent-directory fsync fails. Windows has no equivalent
    bundled directory-fsync step, so the native path instead fails the first
    authority/pathname verification after the handle-relative target rename.
    Both faults occur *inside* BoundTemporaryFile.install() after the target
    name has been acquired and therefore must propagate transition provenance.
    """
    import ppa.secure_write as sw

    conn, _library, target, _donor, expected, _suspect, _rows, _mat, recorded = _case(
        tmp_path, missing=True
    )
    plan = _plan(conn, recorded)
    expected_sha = hashlib.sha256(expected).hexdigest()

    transition_acquired = False
    inject_once = True

    if os.name == "nt":
        real_rename_fd = sw.WindowsDirectoryPin.rename_fd
        real_verify_pathname = sw.WindowsDirectoryPin.verify_pathname

        def rename_then_arm(self, fd: int, destination_name: str, *, replace: bool):
            nonlocal transition_acquired
            real_rename_fd(self, fd, destination_name, replace=replace)
            if destination_name == target.name:
                transition_acquired = True

        def fail_first_post_acquisition_verify(self):
            nonlocal inject_once
            if transition_acquired and inject_once and Path(self.path) == target.parent:
                inject_once = False
                raise sw.SecureWriteError(
                    "forced Windows post-acquisition pathname verification failure"
                )
            return real_verify_pathname(self)

        monkeypatch.setattr(sw.WindowsDirectoryPin, "rename_fd", rename_then_arm)
        monkeypatch.setattr(sw.WindowsDirectoryPin, "verify_pathname", fail_first_post_acquisition_verify)
    else:
        real_atomic = sw.BoundDirectory.rename_child_noreplace_atomic
        real_fsync = sw.BoundDirectory.fsync

        def atomic_then_arm(self, source_name: str, destination_name: str):
            nonlocal transition_acquired
            real_atomic(self, source_name, destination_name)
            if destination_name == target.name and source_name != target.name:
                transition_acquired = True

        def fail_first_post_acquisition_fsync(self):
            nonlocal inject_once
            if transition_acquired and inject_once and Path(self.path) == target.parent:
                inject_once = False
                raise OSError("forced parent fsync failure after target acquisition")
            return real_fsync(self)

        monkeypatch.setattr(sw.BoundDirectory, "rename_child_noreplace_atomic", atomic_then_arm)
        monkeypatch.setattr(sw.BoundDirectory, "fsync", fail_first_post_acquisition_fsync)

    with pytest.raises(RecoveryTargetExecutionError, match="transition|unresolved"):
        execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)

    assert transition_acquired
    assert not inject_once
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_execution_attempts WHERE execution_id=?",
        (plan.execution_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_execution_results WHERE execution_id=?",
        (plan.execution_id,),
    ).fetchone()[0] == 0
    status = inspect_recovery_execution_status(conn, execution_id=plan.execution_id)
    assert status.resolved is False
    assert status.result_state is None

    if os.name == "nt":
        # Native secure-write rollback can prove and remove the just-installed
        # exact object after the injected internal verification failure. The
        # attempt nevertheless remains unresolved because target acquisition did
        # occur and must never be rewritten as a pre-transition abort.
        assert not target.exists()
        assert status.target_state == "missing"
    else:
        # The reviewer's POSIX fsync reproduction leaves the acquired target
        # present with the immutable expected bytes.
        assert target.exists()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha
        assert status.target_state == "matches_expected"


def test_phase1433_missing_restore_post_install_verification_failure_stays_unresolved(
    tmp_path: Path, monkeypatch
) -> None:
    """Failure after a successful install cannot resolve as a pre-transition abort."""
    import ppa.recovery_target_execution as rte

    conn, _library, target, _donor, expected, _suspect, _rows, _mat, recorded = _case(
        tmp_path, missing=True
    )
    plan = _plan(conn, recorded)
    expected_sha = hashlib.sha256(expected).hexdigest()
    real_verify = rte._verify_installed_path
    verification_reached = False

    def fail_after_proving_install(path, **kwargs):
        nonlocal verification_reached
        verification_reached = True
        assert Path(path) == target
        assert target.exists()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha
        # Prove the normal verifier would have accepted this exact installation,
        # then inject failure immediately after that post-transition boundary.
        real_verify(path, **kwargs)
        raise RecoveryTargetExecutionError("forced post-install exact-object verification failure")

    monkeypatch.setattr(rte, "_verify_installed_path", fail_after_proving_install)

    with pytest.raises(RecoveryTargetExecutionError, match="installed|unresolved"):
        execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)

    assert verification_reached
    assert target.exists()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_execution_attempts WHERE execution_id=?",
        (plan.execution_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_execution_results WHERE execution_id=?",
        (plan.execution_id,),
    ).fetchone()[0] == 0
    status = inspect_recovery_execution_status(conn, execution_id=plan.execution_id)
    assert status.resolved is False
    assert status.result_state is None
    assert status.target_state == "matches_expected"

def test_phase143_attempt_and_result_ledgers_are_immutable(tmp_path: Path) -> None:
    conn, _library, _target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)
    result = execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    attempt = conn.execute(
        "SELECT * FROM archive_recovery_target_execution_attempts WHERE execution_id=?", (plan.execution_id,)
    ).fetchone()
    assert attempt["authorization_state"] == AUTHORIZATION_STATE
    assert attempt["target_replacement_authorized"] == 1
    assert attempt["recovery_execution_authorized"] == 1
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute(
            "UPDATE archive_recovery_target_execution_attempts SET note='x' WHERE execution_id=?",
            (plan.execution_id,),
        )
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM archive_recovery_target_execution_attempts WHERE execution_id=?", (plan.execution_id,))
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute(
            "UPDATE archive_recovery_target_execution_results SET detail='x' WHERE result_id=?",
            (result.result_id,),
        )
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM archive_recovery_target_execution_results WHERE result_id=?", (result.result_id,))
    conn.rollback()


def test_phase143_successful_readiness_and_execution_id_cannot_be_replayed(tmp_path: Path) -> None:
    conn, _library, _target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=True)
    execution_id = str(uuid.uuid4())
    plan = _plan(conn, recorded, execution_id)
    execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    with pytest.raises(RecoveryTargetExecutionError, match="execution attempt|already|consumed"):
        _plan(conn, recorded, execution_id)
    with pytest.raises(RecoveryTargetExecutionError, match="execution attempt|already"):
        _plan(conn, recorded, str(uuid.uuid4()))


def test_phase143_unresolved_attempt_blocks_replay_and_status_is_read_only(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_target_execution as rte
    conn, _library, target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)

    def interrupt(_plan):
        raise KeyboardInterrupt()

    monkeypatch.setattr(rte, "_physical_execute", interrupt)
    with pytest.raises(KeyboardInterrupt):
        execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_execution_attempts WHERE execution_id=?", (plan.execution_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_execution_results WHERE execution_id=?", (plan.execution_id,)
    ).fetchone()[0] == 0
    status = inspect_recovery_execution_status(conn, execution_id=plan.execution_id)
    assert status.resolved is False
    assert status.result_state is None
    assert status.target_state == "missing"
    assert not target.exists()
    with pytest.raises(RecoveryTargetExecutionError, match="execution attempt|unresolved"):
        _plan(conn, recorded, str(uuid.uuid4()))


def test_phase143_stale_preview_fails_before_durable_attempt(tmp_path: Path) -> None:
    conn, _library, target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)
    target.write_bytes(b"late-unreviewed-target")
    with pytest.raises(RecoveryTargetExecutionError, match="revalidation|readiness|target|changed"):
        execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_execution_attempts").fetchone()[0] == 0
    assert target.read_bytes() == b"late-unreviewed-target"


def test_phase143_donor_tamper_after_preview_fails_before_durable_attempt(tmp_path: Path) -> None:
    conn, _library, target, _donor, _expected, _suspect, _rows, materialized, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)
    Path(materialized.donor_materialization_path).write_bytes(b"tampered-donor")
    with pytest.raises(RecoveryTargetExecutionError, match="revalidation|donor|readiness"):
        execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_execution_attempts").fetchone()[0] == 0
    assert not target.exists()


def test_phase143_late_target_arrival_is_never_replaced(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_target_execution as rte
    conn, _library, target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)
    real_copy = rte._copy_donor_to_temp

    def copy_then_arrive(p, temp):
        real_copy(p, temp)
        target.write_bytes(b"EXTERNAL-ARRIVAL-MUST-SURVIVE")

    monkeypatch.setattr(rte, "_copy_donor_to_temp", copy_then_arrive)
    result = execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert result.result_state == RESULT_ABORTED
    assert result.source_namespace_changed is False
    assert target.read_bytes() == b"EXTERNAL-ARRIVAL-MUST-SURVIVE"


def test_phase143_result_status_reports_resolved_attempt(tmp_path: Path) -> None:
    conn, _library, _target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)
    result = execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    status = inspect_recovery_execution_status(conn, execution_id=plan.execution_id)
    assert status.resolved is True
    assert status.result_state == result.result_state == RESULT_PLACED
    assert status.target_state == "matches_expected"


def test_phase143_result_sql_constraints_reject_false_success_checkpoint(tmp_path: Path) -> None:
    conn, _library, _target, _donor, _expected, _suspect, rows, _mat, recorded = _case(tmp_path, missing=True)
    plan = _plan(conn, recorded)
    # Authorize a real unresolved attempt, then prove a malformed success result
    # cannot bypass the v41 result constraints.
    import ppa.recovery_target_execution as rte
    rebuilt, _ = rte._authorize_attempt(conn, plan, confirmation=plan.confirmation_phrase, note=None)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO archive_recovery_target_execution_results("
            "result_id,execution_id,readiness_id,target_file_id,result_state,source_namespace_changed,"
            "verify_reconciliation_required,evidence_fingerprint,completed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), rebuilt.execution_id, rebuilt.readiness_id, rows["target.jpg"]["id"],
                RESULT_PLACED, 1, 1, "fake", "now",
            ),
        )
    conn.rollback()


@pytest.mark.skipif(os.name != "nt", reason="native Windows existing-target execution")
def test_phase143_windows_existing_target_replacement_retains_exact_suspect(tmp_path: Path) -> None:
    conn, _library, target, _donor, expected, suspect, rows, _mat, recorded = _case(tmp_path, missing=False)
    plan = _plan(conn, recorded)
    result = execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert result.result_state == RESULT_PLACED
    assert target.read_bytes() == expected
    assert result.suspect_retained_path is not None
    suspect_path = Path(result.suspect_retained_path)
    assert suspect_path.read_bytes() == suspect
    assert hashlib.sha256(suspect).hexdigest() == result.suspect_sha256
    assert conn.execute("SELECT health_status FROM files WHERE id=?", (rows["target.jpg"]["id"],)).fetchone()[0] == "hash_mismatch"
    verify_library(conn)
    assert conn.execute("SELECT health_status FROM files WHERE id=?", (rows["target.jpg"]["id"],)).fetchone()[0] == "ok"


@pytest.mark.skipif(os.name != "nt", reason="native Windows existing-target execution")
def test_phase143_windows_in_place_target_edit_after_preview_is_rejected_before_attempt(tmp_path: Path) -> None:
    conn, _library, target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=False)
    plan = _plan(conn, recorded)
    target.write_bytes(b"changed-in-place")
    with pytest.raises(RecoveryTargetExecutionError, match="revalidation|target|readiness"):
        execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_execution_attempts").fetchone()[0] == 0


def test_phase143_rollback_refuses_when_fresh_parked_path_attestation_disagrees(monkeypatch) -> None:
    """Rollback needs both the original handle and a fresh bound-path view."""
    import ppa.recovery_target_execution as rte

    original = rte._FileSnapshot(
        sha256="ab" * 32,
        size_bytes=123,
        mtime_ns=456,
        fs_device_id="7",
        fs_object_id="9",
        link_count=1,
    )

    class FakeParent:
        def __init__(self) -> None:
            self.renamed = False

        def child_info_or_none(self, name):
            assert name == "target.jpg"
            return None

        def rename_fd(self, fd, destination_name, *, replace):
            self.renamed = True
            raise AssertionError("changed parked object must never be restored")

        def child_identity_or_none(self, name):
            raise AssertionError("identity check must not run after failed fresh attestation")

    parent = FakeParent()
    original_r, original_w = os.pipe()
    fresh_r, fresh_w = os.pipe()
    os.close(original_w)
    os.close(fresh_w)
    calls = 0

    def fake_hash(fd, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert fd == original_r
            return original
        assert fd == fresh_r
        raise RecoveryTargetExecutionError("fresh parked pathname bytes changed")

    monkeypatch.setattr(rte, "_hash_fd_snapshot", fake_hash)
    monkeypatch.setattr(rte, "_open_windows_target_fd", lambda _parent, name: fresh_r)
    try:
        assert not rte._restore_parked_windows_target(
            parent,
            fd=original_r,
            parked_name=".target.jpg.ppa-recovery-test.suspect",
            target_name="target.jpg",
            original=original,
        )
        assert calls == 2
        assert not parent.renamed
    finally:
        os.close(original_r)


def test_phase1435_rollback_post_transition_proof_failure_stays_unresolved(monkeypatch) -> None:
    """Once reverse rename occurs, a failed final byte proof is unresolved."""
    import ppa.recovery_target_execution as rte

    original = rte._FileSnapshot(
        sha256="cd" * 32,
        size_bytes=321,
        mtime_ns=654,
        fs_device_id="17",
        fs_object_id="19",
        link_count=1,
    )

    class FakeParent:
        def __init__(self) -> None:
            self.renamed = False

        def child_info_or_none(self, name):
            assert name == "target.jpg"
            return None

        def rename_fd(self, fd, destination_name, *, replace):
            assert destination_name == "target.jpg"
            assert replace is False
            self.renamed = True

        def child_identity_or_none(self, name):
            assert name == "target.jpg"
            return (17, 19)

    parent = FakeParent()
    original_r, original_w = os.pipe()
    parked_r, parked_w = os.pipe()
    target_r, target_w = os.pipe()
    os.close(original_w)
    os.close(parked_w)
    os.close(target_w)
    calls = 0
    opens = 0

    def fake_hash(fd, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return original
        if calls == 3:
            assert parent.renamed
            raise RecoveryTargetExecutionError("changed after pre-restore proof")
        raise AssertionError("fresh target proof must not proceed after original-handle failure")

    def fake_open(_parent, name):
        nonlocal opens
        opens += 1
        if opens == 1:
            assert name.endswith(".suspect")
            return parked_r
        assert name == "target.jpg"
        return target_r

    monkeypatch.setattr(rte, "_hash_fd_snapshot", fake_hash)
    monkeypatch.setattr(rte, "_open_windows_target_fd", fake_open)
    try:
        with pytest.raises(RecoveryTargetExecutionError, match="reverse rename|final restored-byte proof|unresolved"):
            rte._restore_parked_windows_target(
                parent,
                fd=original_r,
                parked_name=".target.jpg.ppa-recovery-test.suspect",
                target_name="target.jpg",
                original=original,
            )
        assert parent.renamed
        assert calls == 3
    finally:
        os.close(original_r)
        # helper owns/closed parked_r after the pre-restore proof. target_r was
        # never opened because the post-restore original-handle proof failed.
        os.close(target_r)


@pytest.mark.skipif(os.name != "nt", reason="native Windows existing-target rollback")
def test_phase143_windows_preinstall_copy_failure_restores_exact_suspect(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_target_execution as rte
    conn, _library, target, _donor, _expected, suspect, _rows, _mat, recorded = _case(tmp_path, missing=False)
    plan = _plan(conn, recorded)

    def fail_copy(_plan, _temp):
        raise RecoveryTargetExecutionError("forced pre-install copy failure")

    monkeypatch.setattr(rte, "_copy_donor_to_temp", fail_copy)
    result = execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert result.result_state == RESULT_RESTORED
    assert target.read_bytes() == suspect
    assert not Path(f"{target.parent}/.{target.name}.ppa-recovery-{plan.execution_id}.suspect").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows existing-target rollback")
def test_phase143_windows_changed_parked_suspect_is_not_restored_for_liveness(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_target_execution as rte
    conn, _library, target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(tmp_path, missing=False)
    plan = _plan(conn, recorded)

    # Acquire an independent writer before PPA opens/parks the target.  The
    # handle explicitly shares delete, so it remains attached to the same exact
    # object across PPA's native handle-relative rename.
    external_fd = _open_windows_shared_write_fd(target)
    changed = b"EXTERNALLY-CHANGED-PARKED-SUSPECT"

    def alter_then_fail(_plan, _temp):
        os.lseek(external_fd, 0, os.SEEK_SET)
        assert os.write(external_fd, changed) == len(changed)
        os.ftruncate(external_fd, len(changed))
        os.fsync(external_fd)
        assert os.fstat(external_fd).st_size == len(changed)
        raise RecoveryTargetExecutionError("forced after confirmed parked alteration")

    monkeypatch.setattr(rte, "_copy_donor_to_temp", alter_then_fail)
    try:
        with pytest.raises(RecoveryTargetExecutionError, match="parked|unresolved"):
            execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
        suspect_path = target.parent / f".{target.name}.ppa-recovery-{plan.execution_id}.suspect"
        assert suspect_path.read_bytes() == changed
        assert not target.exists()
        assert conn.execute(
            "SELECT COUNT(*) FROM archive_recovery_target_execution_results WHERE execution_id=?",
            (plan.execution_id,),
        ).fetchone()[0] == 0
    finally:
        os.close(external_fd)


@pytest.mark.skipif(os.name != "nt", reason="native Windows rollback finalization")
def test_phase1435_windows_write_after_preproof_before_reverse_rename_stays_unresolved(
    tmp_path: Path, monkeypatch
) -> None:
    """A late writer after both pre-proofs cannot yield an exact-restore result."""
    import ppa.recovery_target_execution as rte

    conn, _library, target, _donor, _expected, _suspect, _rows, _mat, recorded = _case(
        tmp_path, missing=False
    )
    plan = _plan(conn, recorded)
    external_fd = _open_windows_shared_write_fd(target)
    changed = b"CHANGED-AFTER-DUAL-PROOF-BEFORE-REVERSE-RENAME"
    real_rename_fd = rte.WindowsDirectoryPin.rename_fd
    changed_during_restore = False

    def rename_with_late_writer(self, fd, destination_name, *, replace):
        nonlocal changed_during_restore
        if destination_name == target.name and not changed_during_restore:
            # _restore_parked_windows_target calls rename_fd(target_name) only
            # after both pre-restore handle/path proofs have succeeded.  Mutate
            # the already-open exact object at this precise boundary.
            changed_during_restore = True
            os.lseek(external_fd, 0, os.SEEK_SET)
            assert os.write(external_fd, changed) == len(changed)
            os.ftruncate(external_fd, len(changed))
            os.fsync(external_fd)
        return real_rename_fd(self, fd, destination_name, replace=replace)

    def fail_copy(_plan, _temp):
        raise RecoveryTargetExecutionError("forced pre-install failure to enter rollback")

    monkeypatch.setattr(rte.WindowsDirectoryPin, "rename_fd", rename_with_late_writer)
    monkeypatch.setattr(rte, "_copy_donor_to_temp", fail_copy)
    try:
        with pytest.raises(RecoveryTargetExecutionError, match="final restored-byte proof|unresolved|reverse rename"):
            execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
        assert changed_during_restore
        assert target.exists()
        assert target.read_bytes() == changed
        assert conn.execute(
            "SELECT COUNT(*) FROM archive_recovery_target_execution_results WHERE execution_id=?",
            (plan.execution_id,),
        ).fetchone()[0] == 0
        status = inspect_recovery_execution_status(conn, execution_id=plan.execution_id)
        assert not status.resolved
        assert status.result_state is None
        assert status.target_state == "present_other"
        assert status.target_sha256 == hashlib.sha256(changed).hexdigest()
        suspect_path = target.parent / f".{target.name}.ppa-recovery-{plan.execution_id}.suspect"
        assert not suspect_path.exists()
    finally:
        os.close(external_fd)


@pytest.mark.skipif(os.name != "nt", reason="native Windows existing-target no-replace")
def test_phase143_windows_late_target_occupant_survives_and_suspect_stays_parked(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_target_execution as rte
    conn, _library, target, _donor, _expected, suspect, _rows, _mat, recorded = _case(tmp_path, missing=False)
    plan = _plan(conn, recorded)
    real_copy = rte._copy_donor_to_temp

    def arrive_after_park(p, temp):
        real_copy(p, temp)
        target.write_bytes(b"LATE-TARGET-MUST-SURVIVE")

    monkeypatch.setattr(rte, "_copy_donor_to_temp", arrive_after_park)
    with pytest.raises(RecoveryTargetExecutionError, match="parked|unresolved"):
        execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert target.read_bytes() == b"LATE-TARGET-MUST-SURVIVE"
    suspect_path = target.parent / f".{target.name}.ppa-recovery-{plan.execution_id}.suspect"
    assert suspect_path.read_bytes() == suspect


@pytest.mark.skipif(os.name != "nt", reason="native Windows existing-target topology")
def test_phase143_windows_hardlink_alias_blocks_execution_before_attempt(tmp_path: Path) -> None:
    conn, library, target, _donor, _expected, suspect, _rows, _mat, recorded = _case(tmp_path, missing=False)
    plan = _plan(conn, recorded)
    alias = library / "album" / "target-alias.jpg"
    try:
        alias.hardlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("hard links unavailable")
    with pytest.raises(RecoveryTargetExecutionError, match="readiness|hard-link|topology|revalidation"):
        execute_target_replacement(conn, plan, confirmation=plan.confirmation_phrase)
    assert target.read_bytes() == suspect
    assert alias.read_bytes() == suspect
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_execution_attempts").fetchone()[0] == 0
