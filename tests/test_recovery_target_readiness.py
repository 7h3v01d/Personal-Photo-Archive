"""Phase 14.2 target-replacement readiness regressions."""
from __future__ import annotations

from pathlib import Path
import os

import pytest
from PIL import Image

from ppa.db import connect, current_schema_version
from ppa.integrity import verify_library
from ppa.mismatch_investigation import build_mismatch_investigation
from ppa.mismatch_resolution import ACTION_RETAIN_EXPECTED, execute_mismatch_resolution, plan_mismatch_resolution
from ppa.recovery_planning import build_recovery_plan, record_recovery_plan_proposal
from ppa.recovery_preservation import build_preservation_plan, execute_preservation_stage
from ppa.recovery_donor_materialization import build_donor_materialization_plan, execute_donor_materialization
from ppa.recovery_target_readiness import (
    MODE_REPLACE,
    MODE_RESTORE,
    READINESS_STATE,
    RecoveryTargetReadinessError,
    build_target_replacement_readiness,
    record_target_replacement_readiness,
)
from ppa.scanner import scan_library


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def _case(tmp_path: Path, *, missing: bool = False, nested: bool = False):
    library = tmp_path / "library"
    base = library / "album" if nested else library
    target = base / "target.jpg"
    donor = base / "donor.jpg"
    _img(target, "red")
    _img(donor, "red")
    donor_bytes = donor.read_bytes()
    conn = connect(tmp_path / "catalogue.sqlite3")
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
    execute_mismatch_resolution(conn, decision)
    if missing:
        target.unlink()
    proposal = record_recovery_plan_proposal(conn, build_recovery_plan(conn, file_id=rows["target.jpg"]["id"]))
    stage = execute_preservation_stage(conn, build_preservation_plan(conn, proposal_id=proposal.proposal_id))
    materialized = execute_donor_materialization(
        conn, build_donor_materialization_plan(conn, stage_id=stage.stage_id)
    )
    return conn, library, target, donor, donor_bytes, suspect, rows, proposal, stage, materialized


def test_phase142_readiness_is_read_only_and_grants_no_target_authority(tmp_path: Path) -> None:
    conn, _library, target, donor, donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    target_before = target.read_bytes()
    donor_before = donor.read_bytes()
    target_stat = target.stat()
    donor_stat = donor.stat()

    readiness = build_target_replacement_readiness(
        conn, materialization_id=materialized.materialization_id
    )

    assert readiness.readiness_state == READINESS_STATE
    assert readiness.replacement_mode == MODE_REPLACE
    assert readiness.target_replacement_authorized is False
    assert readiness.recovery_execution_authorized is False
    assert readiness.target_link_count == 1
    assert readiness.donor_materialized_sha256 == readiness.expected_sha256
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0
    assert target.read_bytes() == target_before == suspect
    assert donor.read_bytes() == donor_before == donor_bytes
    assert target.stat().st_ino == target_stat.st_ino
    assert donor.stat().st_ino == donor_stat.st_ino


def test_phase142_record_is_immutable_audit_only(tmp_path: Path) -> None:
    conn, _library, target, donor, donor_bytes, suspect, rows, _proposal, _stage, materialized = _case(tmp_path)
    readiness = build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    recorded = record_target_replacement_readiness(conn, readiness, note="reviewed readiness only")

    row = conn.execute(
        "SELECT * FROM archive_recovery_target_readiness WHERE readiness_id=?", (recorded.readiness_id,)
    ).fetchone()
    assert row["readiness_state"] == READINESS_STATE
    assert row["target_replacement_authorized"] == 0
    assert row["recovery_execution_authorized"] == 0
    assert row["replacement_mode"] == MODE_REPLACE
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_bytes
    event = conn.execute(
        "SELECT event_type,detail FROM integrity_events WHERE file_id=? ORDER BY id DESC LIMIT 1",
        (rows["target.jpg"]["id"],),
    ).fetchone()
    assert event["event_type"] == "archive_recovery_target_readiness_recorded"
    assert "NOT authorised" in event["detail"]

    with pytest.raises(Exception, match="immutable"):
        conn.execute(
            "UPDATE archive_recovery_target_readiness SET readiness_state=readiness_state WHERE readiness_id=?",
            (recorded.readiness_id,),
        )
    conn.rollback()
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM archive_recovery_target_readiness WHERE readiness_id=?", (recorded.readiness_id,))
    conn.rollback()


def test_phase142_missing_target_is_restore_readiness_but_does_not_create_target(tmp_path: Path) -> None:
    conn, _library, target, donor, donor_bytes, _suspect, _rows, _proposal, _stage, materialized = _case(
        tmp_path, missing=True
    )
    assert not target.exists()
    readiness = build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert readiness.target_state == "missing"
    assert readiness.replacement_mode == MODE_RESTORE
    assert readiness.target_link_count is None
    assert readiness.target_replacement_authorized is False
    assert not target.exists()
    assert donor.read_bytes() == donor_bytes


def test_phase142_rejects_target_change_after_materialization(tmp_path: Path) -> None:
    conn, _library, target, donor, donor_bytes, _suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    _img(target, "green")
    with pytest.raises(RecoveryTargetReadinessError, match="target|state|changed"):
        build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert donor.read_bytes() == donor_bytes


def test_phase142_rejects_materialized_donor_tamper(tmp_path: Path) -> None:
    conn, _library, target, _donor, _donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    staged = Path(materialized.donor_materialization_path)
    staged.write_bytes(b"tampered-operational-evidence")
    with pytest.raises(RecoveryTargetReadinessError, match="donor|content|size|changed"):
        build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert target.read_bytes() == suspect


def test_phase142_rejects_preservation_evidence_tamper(tmp_path: Path) -> None:
    conn, _library, target, donor, donor_bytes, suspect, _rows, _proposal, stage, materialized = _case(tmp_path)
    preserved = Path(stage.preservation_path)
    preserved.write_bytes(b"tampered-preservation")
    with pytest.raises(RecoveryTargetReadinessError, match="preservation|content|size|changed"):
        build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_bytes


def test_phase142_rejects_late_target_hardlink_topology(tmp_path: Path) -> None:
    conn, library, target, donor, donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    alias = library / "target-hardlink-alias.jpg"
    try:
        alias.hardlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("hard links unavailable")
    before_mode = target.stat().st_mode
    with pytest.raises(RecoveryTargetReadinessError, match="hard-link|topology"):
        build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert target.read_bytes() == suspect
    assert alias.read_bytes() == suspect
    assert target.stat().st_mode == before_mode
    assert donor.read_bytes() == donor_bytes


def test_phase142_rejects_replaced_target_parent_directory_object(tmp_path: Path) -> None:
    conn, library, target, donor, donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    parked = tmp_path / "library-parked"
    replacement = tmp_path / "replacement-library"
    replacement.mkdir()
    library.rename(parked)
    replacement.rename(library)
    # Put byte-identical target/donor names under the replacement path so a mere
    # pathname/content check would appear superficially plausible.
    (library / "target.jpg").write_bytes(suspect)
    (library / "donor.jpg").write_bytes(donor_bytes)
    with pytest.raises(RecoveryTargetReadinessError, match="parent|source-tree|directory|identity|rescan"):
        build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert (parked / "target.jpg").read_bytes() == suspect
    assert (parked / "donor.jpg").read_bytes() == donor_bytes


def test_phase142_record_revalidates_and_refuses_stale_snapshot(tmp_path: Path) -> None:
    conn, _library, target, donor, donor_bytes, _suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    readiness = build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    _img(target, "green")
    with pytest.raises(RecoveryTargetReadinessError, match="target|changed|state"):
        record_target_replacement_readiness(conn, readiness)
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0
    assert donor.read_bytes() == donor_bytes



def test_phase142_accepts_committed_embedded_donor_manifest_without_filesystem_write(tmp_path: Path, monkeypatch) -> None:
    import ppa.recovery_donor_materialization as rdm

    # Build through Phase 14.0, then strand the completed donor before manifest
    # creation so orphan reconciliation commits the canonical manifest payload
    # in SQLite rather than creating a filesystem manifest.
    library = tmp_path / "library"
    target = library / "target.jpg"
    donor = library / "donor.jpg"
    _img(target, "red")
    _img(donor, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    rows = {r["filename"]: r for r in conn.execute("SELECT * FROM files")}
    _img(target, "blue")
    assert verify_library(conn).mismatches == 1
    inv = build_mismatch_investigation(conn, rows["target.jpg"]["id"], thumbnail_cache_dir=tmp_path / "thumbs")
    execute_mismatch_resolution(
        conn,
        plan_mismatch_resolution(
            conn, file_id=inv.file_id, action=ACTION_RETAIN_EXPECTED,
            reviewed_expected_revision_id=inv.expected_revision_id,
            reviewed_expected_sha256=inv.expected_sha256,
            reviewed_current_state=inv.current_state,
            reviewed_current_sha256=inv.current_observed_sha256,
            reviewed_observation_id=inv.verify_observation_id,
        ),
    )
    proposal = record_recovery_plan_proposal(conn, build_recovery_plan(conn, file_id=rows["target.jpg"]["id"]))
    stage = execute_preservation_stage(conn, build_preservation_plan(conn, proposal_id=proposal.proposal_id))
    plan = build_donor_materialization_plan(conn, stage_id=stage.stage_id)

    def interrupt_manifest(_path: Path, _payload: dict, *_args) -> str:
        raise KeyboardInterrupt()

    real_manifest = rdm._write_json_manifest
    monkeypatch.setattr(rdm, "_write_json_manifest", interrupt_manifest)
    with pytest.raises(KeyboardInterrupt):
        execute_donor_materialization(conn, plan)
    monkeypatch.setattr(rdm, "_write_json_manifest", real_manifest)
    adopted = rdm.reconcile_donor_materialization_orphans(conn, stage_id=stage.stage_id)
    assert adopted["manifest_storage"] == "catalogue_embedded"
    row = conn.execute(
        "SELECT materialization_id,donor_manifest_path FROM archive_recovery_donor_materializations WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert row is not None
    assert not Path(row["donor_manifest_path"]).exists()

    readiness = build_target_replacement_readiness(conn, materialization_id=row["materialization_id"])
    assert readiness.donor_manifest_storage == "catalogue_embedded"
    assert readiness.target_replacement_authorized is False
    assert not Path(row["donor_manifest_path"]).exists()

def test_phase142_readiness_authority_columns_remain_zero_only_under_schema_v41(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")
    assert current_schema_version(conn) == 41
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='archive_recovery_target_readiness'"
    ).fetchone()[0]
    assert "target_replacement_authorized" in sql
    assert "CHECK(target_replacement_authorized=0)" in sql
    assert "recovery_execution_authorized" in sql
    assert "CHECK(recovery_execution_authorized=0)" in sql


def test_phase1421_target_change_during_evidence_attestation_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """Final destination attestation must catch a target changed after initial checks."""
    import ppa.recovery_target_readiness as rtr

    conn, _library, target, donor, donor_bytes, _suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    real_attest = rtr._attest_operational_file
    changed = False

    def mutate_target_then_attest(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            _img(target, "green")
        return real_attest(*args, **kwargs)

    monkeypatch.setattr(rtr, "_attest_operational_file", mutate_target_then_attest)
    with pytest.raises(RecoveryTargetReadinessError, match="target|destination|changed|state"):
        build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert changed
    assert donor.read_bytes() == donor_bytes
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0


def test_phase1421_target_parent_change_during_evidence_attestation_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """A Library parent swapped after initial authority checks must fail finalisation."""
    import ppa.recovery_target_readiness as rtr

    conn, library, target, donor, donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    real_attest = rtr._attest_operational_file
    parked = tmp_path / "library-parked-during-readiness"
    replacement = tmp_path / "library-replacement-during-readiness"
    replacement.mkdir()
    changed = False

    def swap_parent_then_attest(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            library.rename(parked)
            replacement.rename(library)
            (library / "target.jpg").write_bytes(suspect)
            (library / "donor.jpg").write_bytes(donor_bytes)
        return real_attest(*args, **kwargs)

    monkeypatch.setattr(rtr, "_attest_operational_file", swap_parent_then_attest)
    with pytest.raises(RecoveryTargetReadinessError, match="parent|source-tree|directory|changed|rescan"):
        build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert changed
    assert (parked / "target.jpg").read_bytes() == suspect
    assert (parked / "donor.jpg").read_bytes() == donor_bytes
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0


def test_phase1421_record_rebuild_destination_race_commits_no_stale_row_or_event(tmp_path: Path, monkeypatch) -> None:
    """Record-time rebuild inherits final destination attestation and stays atomic."""
    import ppa.recovery_target_readiness as rtr

    conn, _library, target, donor, donor_bytes, _suspect, rows, _proposal, _stage, materialized = _case(tmp_path)
    displayed = build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    event_count_before = conn.execute(
        "SELECT COUNT(*) FROM integrity_events WHERE file_id=? AND event_type='archive_recovery_target_readiness_recorded'",
        (rows["target.jpg"]["id"],),
    ).fetchone()[0]

    real_attest = rtr._attest_operational_file
    changed = False

    def mutate_target_during_record_rebuild(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            _img(target, "green")
        return real_attest(*args, **kwargs)

    monkeypatch.setattr(rtr, "_attest_operational_file", mutate_target_during_record_rebuild)
    with pytest.raises(RecoveryTargetReadinessError, match="target|destination|changed|state"):
        record_target_replacement_readiness(conn, displayed)

    assert changed
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0
    event_count_after = conn.execute(
        "SELECT COUNT(*) FROM integrity_events WHERE file_id=? AND event_type='archive_recovery_target_readiness_recorded'",
        (rows["target.jpg"]["id"],),
    ).fetchone()[0]
    assert event_count_after == event_count_before
    assert donor.read_bytes() == donor_bytes


def test_phase1421_target_link_count_metadata_must_match_observed_identity(tmp_path: Path, monkeypatch) -> None:
    """The lstat supplying nlink cannot describe a replacement target object."""
    import ppa.recovery_target_readiness as rtr

    conn, _library, target, donor, donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(tmp_path)
    real_observe = rtr.observe_stable_image
    parked = tmp_path / "target-observed-object.jpg"
    replacement = tmp_path / "target-replacement-object.jpg"
    _img(replacement, "green")
    swapped = False

    def observe_then_swap(path: Path, *args, **kwargs):
        nonlocal swapped
        result = real_observe(path, *args, **kwargs)
        if Path(path) == target and not swapped:
            swapped = True
            target.rename(parked)
            replacement.rename(target)
        return result

    monkeypatch.setattr(rtr, "observe_stable_image", observe_then_swap)
    with pytest.raises(RecoveryTargetReadinessError, match="identity changed|target.*changed"):
        build_target_replacement_readiness(conn, materialization_id=materialized.materialization_id)
    assert swapped
    assert parked.read_bytes() == suspect
    assert donor.read_bytes() == donor_bytes
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0


def test_phase1422_rejects_substituted_library_root_with_transplanted_known_parent(tmp_path: Path) -> None:
    """A genuine known child cannot authenticate a substituted registered Library root."""
    conn, library, target, donor, donor_bytes, suspect, rows, _proposal, _stage, materialized = _case(
        tmp_path, nested=True
    )
    displayed = build_target_replacement_readiness(
        conn, materialization_id=materialized.materialization_id
    )
    root_row = conn.execute(
        "SELECT root_fs_device_id,root_fs_object_id FROM libraries WHERE id=?",
        (rows["target.jpg"]["library_id"],),
    ).fetchone()
    album_stat = (library / "album").stat()
    album_row = conn.execute(
        "SELECT fs_device_id,fs_object_id FROM library_directory_identities "
        "WHERE library_id=? AND fs_device_id=? AND fs_object_id=?",
        (
            rows["target.jpg"]["library_id"],
            str(album_stat.st_dev),
            str(album_stat.st_ino),
        ),
    ).fetchone()
    assert root_row is not None
    assert album_row is not None

    parked = tmp_path / "library-parked-root"
    library.rename(parked)
    library.mkdir()
    (parked / "album").rename(library / "album")

    current_root = library.stat()
    current_album = (library / "album").stat()
    assert (str(current_root.st_dev), str(current_root.st_ino)) != (
        str(root_row["root_fs_device_id"]), str(root_row["root_fs_object_id"])
    )
    assert (str(current_album.st_dev), str(current_album.st_ino)) == (
        str(album_row["fs_device_id"]), str(album_row["fs_object_id"])
    )

    event_count_before = conn.execute(
        "SELECT COUNT(*) FROM integrity_events WHERE file_id=? "
        "AND event_type='archive_recovery_target_readiness_recorded'",
        (rows["target.jpg"]["id"],),
    ).fetchone()[0]
    row_count_before = conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_readiness"
    ).fetchone()[0]

    with pytest.raises(RecoveryTargetReadinessError, match="Library root.*identity changed|rescan/review"):
        build_target_replacement_readiness(
            conn, materialization_id=materialized.materialization_id
        )
    with pytest.raises(RecoveryTargetReadinessError, match="Library root.*identity changed|rescan/review"):
        record_target_replacement_readiness(conn, displayed)

    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_readiness"
    ).fetchone()[0] == row_count_before
    assert conn.execute(
        "SELECT COUNT(*) FROM integrity_events WHERE file_id=? "
        "AND event_type='archive_recovery_target_readiness_recorded'",
        (rows["target.jpg"]["id"],),
    ).fetchone()[0] == event_count_before
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_bytes


def test_phase1422_library_root_change_during_evidence_attestation_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """Final topology attestation must re-prove the registered Library root object."""
    import ppa.recovery_target_readiness as rtr

    conn, library, target, donor, donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(
        tmp_path, nested=True
    )
    real_attest = rtr._attest_operational_file
    parked = tmp_path / "library-parked-during-root-attestation"
    changed = False

    def swap_root_keep_genuine_parent_then_attest(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            library.rename(parked)
            library.mkdir()
            (parked / "album").rename(library / "album")
        return real_attest(*args, **kwargs)

    monkeypatch.setattr(rtr, "_attest_operational_file", swap_root_keep_genuine_parent_then_attest)
    with pytest.raises(RecoveryTargetReadinessError, match="Library root.*identity changed|rescan/review"):
        build_target_replacement_readiness(
            conn, materialization_id=materialized.materialization_id
        )

    assert changed
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_bytes
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0


def test_phase1423_final_root_pin_detects_swap_during_target_observation(tmp_path: Path, monkeypatch) -> None:
    """The exact registered root stays pinned across the final target hash."""
    import ppa.recovery_target_readiness as rtr

    conn, library, target, donor, donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(
        tmp_path, nested=True
    )
    real_observe = rtr.observe_stable_image
    target_observations = 0
    swapped = False
    rename_blocked_by_pin = False
    parked = tmp_path / "library-parked-final-root-race"

    def swap_root_during_final_target_hash(path: Path, *args, **kwargs):
        nonlocal target_observations, swapped, rename_blocked_by_pin
        if Path(path) == target:
            target_observations += 1
            if target_observations == 2 and not swapped and not rename_blocked_by_pin:
                try:
                    library.rename(parked)
                except PermissionError as exc:
                    # On native Windows the concurrently-open root + descendant
                    # directory pins can make the ancestor rename itself fail with
                    # ERROR_ACCESS_DENIED.  That is a stronger safe outcome than
                    # post-hash detection: the stale topology was never created.
                    if os.name == "nt" and getattr(exc, "winerror", None) == 5:
                        rename_blocked_by_pin = True
                    else:
                        raise
                else:
                    swapped = True
                    library.mkdir()
                    # Preserve the genuine historically-known target parent and the
                    # exact target object beneath a substituted root pathname.
                    (parked / "album").rename(library / "album")
        return real_observe(path, *args, **kwargs)

    monkeypatch.setattr(rtr, "observe_stable_image", swap_root_during_final_target_hash)
    if os.name == "nt":
        readiness = build_target_replacement_readiness(
            conn, materialization_id=materialized.materialization_id
        )
        assert rename_blocked_by_pin
        assert not swapped
        assert readiness.readiness_state == READINESS_STATE
    else:
        with pytest.raises(RecoveryTargetReadinessError, match="bound destination topology|Library root|rescan/review"):
            build_target_replacement_readiness(
                conn, materialization_id=materialized.materialization_id
            )
        assert swapped
        assert not rename_blocked_by_pin

    assert target_observations == 2
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_bytes
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0


def test_phase1423_final_parent_pin_detects_swap_during_target_observation(tmp_path: Path, monkeypatch) -> None:
    """The exact target parent stays pinned across the final target hash."""
    import ppa.recovery_target_readiness as rtr

    conn, library, target, donor, donor_bytes, suspect, _rows, _proposal, _stage, materialized = _case(
        tmp_path, nested=True
    )
    real_observe = rtr.observe_stable_image
    target_observations = 0
    swapped = False
    parent = library / "album"
    parked_parent = library / "album-parked-final-parent-race"

    def swap_parent_during_final_target_hash(path: Path, *args, **kwargs):
        nonlocal target_observations, swapped
        if Path(path) == target:
            target_observations += 1
            if target_observations == 2 and not swapped:
                swapped = True
                parent.rename(parked_parent)
                parent.mkdir()
                # Transplant the exact target object into a replacement parent.
                (parked_parent / "target.jpg").rename(parent / "target.jpg")
        return real_observe(path, *args, **kwargs)

    monkeypatch.setattr(rtr, "observe_stable_image", swap_parent_during_final_target_hash)
    with pytest.raises(RecoveryTargetReadinessError, match="bound destination topology|parent|rescan/review"):
        build_target_replacement_readiness(
            conn, materialization_id=materialized.materialization_id
        )

    assert swapped
    assert target_observations == 2
    assert target.read_bytes() == suspect
    assert (parked_parent / "donor.jpg").read_bytes() == donor_bytes
    assert conn.execute("SELECT COUNT(*) FROM archive_recovery_target_readiness").fetchone()[0] == 0


def test_phase1423_record_rebuild_root_pin_race_commits_no_row_or_event(tmp_path: Path, monkeypatch) -> None:
    """Record-time rebuild cannot commit after a root swap during final target hashing."""
    import ppa.recovery_target_readiness as rtr

    conn, library, target, donor, donor_bytes, suspect, rows, _proposal, _stage, materialized = _case(
        tmp_path, nested=True
    )
    displayed = build_target_replacement_readiness(
        conn, materialization_id=materialized.materialization_id
    )
    row_count_before = conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_readiness"
    ).fetchone()[0]
    event_count_before = conn.execute(
        "SELECT COUNT(*) FROM integrity_events WHERE file_id=? "
        "AND event_type='archive_recovery_target_readiness_recorded'",
        (rows["target.jpg"]["id"],),
    ).fetchone()[0]

    real_observe = rtr.observe_stable_image
    target_observations = 0
    swapped = False
    rename_blocked_by_pin = False
    parked = tmp_path / "library-parked-record-root-race"

    def swap_root_during_record_final_target_hash(path: Path, *args, **kwargs):
        nonlocal target_observations, swapped, rename_blocked_by_pin
        if Path(path) == target:
            target_observations += 1
            if target_observations == 2 and not swapped and not rename_blocked_by_pin:
                try:
                    library.rename(parked)
                except PermissionError as exc:
                    if os.name == "nt" and getattr(exc, "winerror", None) == 5:
                        rename_blocked_by_pin = True
                    else:
                        raise
                else:
                    swapped = True
                    library.mkdir()
                    (parked / "album").rename(library / "album")
        return real_observe(path, *args, **kwargs)

    monkeypatch.setattr(rtr, "observe_stable_image", swap_root_during_record_final_target_hash)
    if os.name == "nt":
        recorded = record_target_replacement_readiness(conn, displayed)
        assert rename_blocked_by_pin
        assert not swapped
        # The attempted topology attack was denied by the native pins, so the
        # rebuild remained truthful and a normal audit-only row may be recorded.
        assert recorded.readiness_id == displayed.readiness_id
        assert recorded.materialization_id == displayed.materialization_id
        assert recorded.file_id == displayed.file_id
        assert recorded.evidence_fingerprint == displayed.evidence_fingerprint
        recorded_row = conn.execute(
            "SELECT readiness_state,target_replacement_authorized,recovery_execution_authorized "
            "FROM archive_recovery_target_readiness WHERE readiness_id=?",
            (recorded.readiness_id,),
        ).fetchone()
        assert recorded_row is not None
        assert recorded_row[0] == READINESS_STATE
        assert int(recorded_row[1]) == 0
        assert int(recorded_row[2]) == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM archive_recovery_target_readiness"
        ).fetchone()[0] == row_count_before + 1
        assert conn.execute(
            "SELECT COUNT(*) FROM integrity_events WHERE file_id=? "
            "AND event_type='archive_recovery_target_readiness_recorded'",
            (rows["target.jpg"]["id"],),
        ).fetchone()[0] == event_count_before + 1
    else:
        with pytest.raises(RecoveryTargetReadinessError, match="bound destination topology|Library root|rescan/review"):
            record_target_replacement_readiness(conn, displayed)
        assert swapped
        assert not rename_blocked_by_pin
        assert conn.execute(
            "SELECT COUNT(*) FROM archive_recovery_target_readiness"
        ).fetchone()[0] == row_count_before
        assert conn.execute(
            "SELECT COUNT(*) FROM integrity_events WHERE file_id=? "
            "AND event_type='archive_recovery_target_readiness_recorded'",
            (rows["target.jpg"]["id"],),
        ).fetchone()[0] == event_count_before

    assert target_observations == 2
    assert target.read_bytes() == suspect
    assert donor.read_bytes() == donor_bytes


def test_phase1423_record_rebuild_parent_pin_race_commits_no_row_or_event(tmp_path: Path, monkeypatch) -> None:
    """Record-time rebuild cannot commit after a parent swap during final target hashing."""
    import ppa.recovery_target_readiness as rtr

    conn, library, target, _donor, donor_bytes, suspect, rows, _proposal, _stage, materialized = _case(
        tmp_path, nested=True
    )
    displayed = build_target_replacement_readiness(
        conn, materialization_id=materialized.materialization_id
    )
    row_count_before = conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_readiness"
    ).fetchone()[0]
    event_count_before = conn.execute(
        "SELECT COUNT(*) FROM integrity_events WHERE file_id=? "
        "AND event_type='archive_recovery_target_readiness_recorded'",
        (rows["target.jpg"]["id"],),
    ).fetchone()[0]

    real_observe = rtr.observe_stable_image
    target_observations = 0
    swapped = False
    parent = library / "album"
    parked_parent = library / "album-parked-record-parent-race"

    def swap_parent_during_record_final_target_hash(path: Path, *args, **kwargs):
        nonlocal target_observations, swapped
        if Path(path) == target:
            target_observations += 1
            if target_observations == 2 and not swapped:
                swapped = True
                parent.rename(parked_parent)
                parent.mkdir()
                (parked_parent / "target.jpg").rename(parent / "target.jpg")
        return real_observe(path, *args, **kwargs)

    monkeypatch.setattr(rtr, "observe_stable_image", swap_parent_during_record_final_target_hash)
    with pytest.raises(RecoveryTargetReadinessError, match="bound destination topology|parent|rescan/review"):
        record_target_replacement_readiness(conn, displayed)

    assert swapped
    assert target_observations == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM archive_recovery_target_readiness"
    ).fetchone()[0] == row_count_before
    assert conn.execute(
        "SELECT COUNT(*) FROM integrity_events WHERE file_id=? "
        "AND event_type='archive_recovery_target_readiness_recorded'",
        (rows["target.jpg"]["id"],),
    ).fetchone()[0] == event_count_before
    assert target.read_bytes() == suspect
    assert (parked_parent / "donor.jpg").read_bytes() == donor_bytes
