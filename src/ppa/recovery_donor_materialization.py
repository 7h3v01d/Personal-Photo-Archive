"""Phase 14.1 — verified donor materialization into protected recovery staging.

This slice copies the already-qualified *expected* donor bytes into the immutable
Phase-14 preservation stage.  It never writes to the target source path and does
not authorise target replacement.  The Phase-14.0 checkpoint remains immutable;
Phase 14.1 appends a separate evidence record and manifest.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from sqlite3 import Connection

from ppa.hashing import sha256_file
from ppa.physical_observation import PhysicalObservationError, StableFileObservation, observe_stable_image
from ppa.recovery_planning import RecoveryPlanningError, build_recovery_plan
from ppa.recovery_preservation import (
    RecoveryPreservationError,
    _canonical,
    _chmod_read_only,
    _copy_preserved_bytes,
    _directory_identity,
    _fingerprint,
    _free_bytes_for,
    _fsync_directory,
    _regular_file_identity,
    _same_observation,
    _validate_preservation_root,
    _validated_stage_id,
)

DONOR_PLAN_SCHEMA = "ppa-recovery-donor-materialization-plan/1"
DONOR_RESULT_SCHEMA = "ppa-recovery-donor-materialization/1"
DONOR_MANIFEST_SCHEMA = "ppa-recovery-donor-manifest/1"
MATERIALIZED = "verified_donor_materialized"
_MIN_FREE_RESERVE_BYTES = 8 * 1024 * 1024


class RecoveryDonorMaterializationError(ValueError):
    """A verified donor cannot safely be materialized from current evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _stage_row(conn: Connection, stage_id: str):
    return conn.execute(
        "SELECT * FROM archive_recovery_preservation_stages WHERE stage_id=?",
        (stage_id,),
    ).fetchone()


def _obs_payload(obs: StableFileObservation) -> dict:
    return {
        "state": obs.state,
        "sha256": obs.sha256,
        "size_bytes": obs.size_bytes,
        "mtime_ns": obs.mtime_ns,
        "fs_device_id": obs.fs_device_id,
        "fs_object_id": obs.fs_object_id,
        "width_px": obs.width_px,
        "height_px": obs.height_px,
        "mime_type": obs.mime_type,
    }


def _stage_dir_from_row(conn: Connection, row) -> tuple[Path, Path]:
    try:
        sid = _validated_stage_id(str(row["stage_id"]))
        root = _validate_preservation_root(conn, Path(row["preservation_root"]))
    except RecoveryPreservationError as exc:
        raise RecoveryDonorMaterializationError(str(exc)) from exc
    stage_dir = root / sid
    if os.path.dirname(_canonical(stage_dir)) != _canonical(root):
        raise RecoveryDonorMaterializationError("recorded recovery stage escaped its preservation root")
    if not stage_dir.is_dir() or stage_dir.is_symlink():
        raise RecoveryDonorMaterializationError("recorded recovery stage directory is unavailable or unsafe")
    _directory_identity(stage_dir)
    return root, stage_dir


def _verify_committed_stage_evidence(row, stage_dir: Path) -> None:
    manifest = Path(row["manifest_path"])
    if manifest.parent != stage_dir or manifest.is_symlink() or not manifest.is_file():
        raise RecoveryDonorMaterializationError("preservation manifest path is unavailable or unsafe")
    if sha256_file(manifest) != row["manifest_sha256"]:
        raise RecoveryDonorMaterializationError("preservation manifest changed after the Phase-14 checkpoint")
    if row["stage_state"] == "suspect_bytes_preserved":
        preserved = Path(row["preservation_path"])
        if preserved.parent != stage_dir or preserved.is_symlink() or not preserved.is_file():
            raise RecoveryDonorMaterializationError("preserved suspect evidence is unavailable or unsafe")
        if sha256_file(preserved) != row["preserved_sha256"]:
            raise RecoveryDonorMaterializationError("preserved suspect evidence changed after the Phase-14 checkpoint")
        if int(preserved.stat().st_size) != int(row["preserved_size_bytes"]):
            raise RecoveryDonorMaterializationError("preserved suspect evidence size changed after the Phase-14 checkpoint")


def _materialization_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    raw = str(value).strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise RecoveryDonorMaterializationError("donor materialization ID must be a canonical UUID") from exc
    if raw != str(parsed):
        raise RecoveryDonorMaterializationError("donor materialization ID must be a canonical UUID")
    return raw


@dataclass(frozen=True)
class RecoveryDonorMaterializationPlan:
    schema: str
    materialization_id: str
    stage_id: str
    proposal_id: str
    file_id: str
    donor_file_id: str
    donor_path: str
    expected_revision_id: str
    expected_sha256: str
    recovery_intent_resolution_id: str
    preservation_root: str
    stage_dir: str
    donor_materialization_path: str
    donor_manifest_path: str
    donor_observed_sha256: str
    donor_size_bytes: int
    donor_mtime_ns: int
    donor_fs_device_id: str | None
    donor_fs_object_id: str | None
    phase13_evidence_fingerprint: str
    phase14_stage_fingerprint: str
    available_bytes: int
    required_bytes: int
    materialization_authorized: bool
    target_replacement_authorized: bool
    recovery_execution_authorized: bool
    evidence_fingerprint: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


@dataclass(frozen=True)
class RecoveryDonorMaterializationResult:
    schema: str
    materialization_id: str
    stage_id: str
    materialization_state: str
    donor_materialization_path: str
    donor_materialized_sha256: str
    donor_materialized_size_bytes: int
    donor_manifest_path: str
    donor_manifest_sha256: str
    target_replacement_performed: bool
    recovery_execution_authorized: bool
    materialized_at: str
    evidence_fingerprint: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


def build_donor_materialization_plan(
    conn: Connection,
    *,
    stage_id: str,
    materialization_id: str | None = None,
) -> RecoveryDonorMaterializationPlan:
    sid = _validated_stage_id(stage_id)
    row = _stage_row(conn, sid)
    if row is None:
        raise RecoveryDonorMaterializationError("unknown committed recovery preservation stage")
    if conn.execute(
        "SELECT 1 FROM archive_recovery_donor_materializations WHERE stage_id=? LIMIT 1", (sid,)
    ).fetchone() is not None:
        raise RecoveryDonorMaterializationError("this preservation stage already has a verified donor materialization")

    root, stage_dir = _stage_dir_from_row(conn, row)
    _verify_committed_stage_evidence(row, stage_dir)

    try:
        rebuilt = build_recovery_plan(
            conn,
            file_id=str(row["target_file_id"]),
            donor_file_id=str(row["donor_file_id"]),
            proposal_id=str(row["proposal_id"]),
        )
    except RecoveryPlanningError as exc:
        raise RecoveryDonorMaterializationError(
            "frozen recovery proposal is no longer valid; refresh recovery planning"
        ) from exc
    if rebuilt.evidence_fingerprint != row["phase13_evidence_fingerprint"]:
        raise RecoveryDonorMaterializationError("Phase-13 recovery evidence changed after preservation staging")
    if rebuilt.recovery_intent_resolution_id != row["recovery_intent_resolution_id"]:
        raise RecoveryDonorMaterializationError("human recovery intent changed after preservation staging")

    try:
        donor = observe_stable_image(Path(rebuilt.donor_path), expected_sha256=rebuilt.expected_sha256)
        target = observe_stable_image(Path(rebuilt.target_path), expected_sha256=rebuilt.expected_sha256)
    except PhysicalObservationError as exc:
        raise RecoveryDonorMaterializationError("source evidence changed while donor readiness was observed") from exc
    if donor.state != "matches_expected" or donor.sha256 != rebuilt.expected_sha256:
        raise RecoveryDonorMaterializationError("donor no longer reproduces the immutable expected revision")
    if target.state == "matches_expected":
        raise RecoveryDonorMaterializationError("target already reproduces expected bytes; run Verify instead of recovery")
    if target.state != row["target_state"] or target.sha256 != row["target_observed_sha256"]:
        raise RecoveryDonorMaterializationError("target physical state changed after the preservation checkpoint")
    if target.size_bytes != row["target_size_bytes"] or target.mtime_ns != row["target_mtime_ns"]:
        raise RecoveryDonorMaterializationError("target physical evidence changed after the preservation checkpoint")

    mid = _materialization_id(materialization_id)
    suffix = Path(rebuilt.donor_path).suffix or ".bin"
    donor_path = stage_dir / ("expected-donor" + suffix)
    donor_manifest = stage_dir / "donor-materialization.json"
    if donor_path.exists() or donor_path.is_symlink() or donor_manifest.exists() or donor_manifest.is_symlink():
        raise RecoveryDonorMaterializationError("donor materialization destination already exists")
    required = int(donor.size_bytes or 0)
    available = _free_bytes_for(stage_dir)
    if available < required + _MIN_FREE_RESERVE_BYTES:
        raise RecoveryDonorMaterializationError("insufficient operational storage for verified donor materialization")

    evidence = {
        "materialization_id": mid,
        "stage_id": sid,
        "proposal_id": str(row["proposal_id"]),
        "file_id": rebuilt.file_id,
        "donor_file_id": rebuilt.donor_file_id,
        "expected_revision_id": rebuilt.expected_revision_id,
        "expected_sha256": rebuilt.expected_sha256,
        "recovery_intent_resolution_id": rebuilt.recovery_intent_resolution_id,
        "phase13_evidence_fingerprint": str(row["phase13_evidence_fingerprint"]),
        "phase14_stage_fingerprint": str(row["evidence_fingerprint"]),
        "donor": _obs_payload(donor),
        "target": _obs_payload(target),
        "donor_materialization_path": str(donor_path),
        "donor_manifest_path": str(donor_manifest),
        "materialization_authorized": False,
        "target_replacement_authorized": False,
        "recovery_execution_authorized": False,
    }
    return RecoveryDonorMaterializationPlan(
        schema=DONOR_PLAN_SCHEMA,
        materialization_id=mid,
        stage_id=sid,
        proposal_id=str(row["proposal_id"]),
        file_id=rebuilt.file_id,
        donor_file_id=rebuilt.donor_file_id,
        donor_path=rebuilt.donor_path,
        expected_revision_id=rebuilt.expected_revision_id,
        expected_sha256=rebuilt.expected_sha256,
        recovery_intent_resolution_id=rebuilt.recovery_intent_resolution_id,
        preservation_root=str(root),
        stage_dir=str(stage_dir),
        donor_materialization_path=str(donor_path),
        donor_manifest_path=str(donor_manifest),
        donor_observed_sha256=str(donor.sha256),
        donor_size_bytes=int(donor.size_bytes or 0),
        donor_mtime_ns=int(donor.mtime_ns or 0),
        donor_fs_device_id=donor.fs_device_id,
        donor_fs_object_id=donor.fs_object_id,
        phase13_evidence_fingerprint=str(row["phase13_evidence_fingerprint"]),
        phase14_stage_fingerprint=str(row["evidence_fingerprint"]),
        available_bytes=available,
        required_bytes=required,
        materialization_authorized=False,
        target_replacement_authorized=False,
        recovery_execution_authorized=False,
        evidence_fingerprint=_fingerprint(evidence),
    )


def _write_json_manifest(path: Path, payload: dict) -> str:
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="donor-manifest.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def _cleanup_owned(paths: list[Path], stage_dir: Path, stage_identity: tuple[int, int]) -> None:
    try:
        if _directory_identity(stage_dir) != stage_identity:
            return
    except Exception:
        return
    for path in paths:
        try:
            if path.parent != stage_dir:
                continue
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            if path.exists() and path.is_file():
                path.unlink(missing_ok=True)
        except OSError:
            pass


def execute_donor_materialization(
    conn: Connection,
    plan: RecoveryDonorMaterializationPlan,
    *,
    note: str | None = None,
) -> RecoveryDonorMaterializationResult:
    if plan.schema != DONOR_PLAN_SCHEMA:
        raise RecoveryDonorMaterializationError("invalid donor materialization plan schema")
    if plan.materialization_authorized or plan.target_replacement_authorized or plan.recovery_execution_authorized:
        raise RecoveryDonorMaterializationError("Phase 14.1 plan contains forbidden recovery authority")
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise RecoveryDonorMaterializationError("donor-materialization note is too long")

    owned: list[Path] = []
    stage_dir: Path | None = None
    stage_identity: tuple[int, int] | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        rebuilt = build_donor_materialization_plan(
            conn, stage_id=plan.stage_id, materialization_id=plan.materialization_id
        )
        if rebuilt.evidence_fingerprint != plan.evidence_fingerprint:
            raise RecoveryDonorMaterializationError("donor materialization plan is stale; rebuild it")
        stage_dir = Path(rebuilt.stage_dir)
        stage_identity = _directory_identity(stage_dir)

        source = Path(rebuilt.donor_path)
        try:
            donor_before = observe_stable_image(source, expected_sha256=rebuilt.expected_sha256)
        except PhysicalObservationError as exc:
            raise RecoveryDonorMaterializationError("donor changed before materialization") from exc
        if donor_before.state != "matches_expected" or donor_before.sha256 != rebuilt.expected_sha256:
            raise RecoveryDonorMaterializationError("donor does not reproduce expected bytes at execution time")

        free_now = _free_bytes_for(stage_dir)
        if free_now < rebuilt.required_bytes + _MIN_FREE_RESERVE_BYTES:
            raise RecoveryDonorMaterializationError("insufficient operational storage at donor materialization time")

        destination = Path(rebuilt.donor_materialization_path)
        manifest = Path(rebuilt.donor_manifest_path)
        if destination.parent != stage_dir or manifest.parent != stage_dir:
            raise RecoveryDonorMaterializationError("donor materialization path escaped the committed stage")
        if destination.exists() or destination.is_symlink() or manifest.exists() or manifest.is_symlink():
            raise RecoveryDonorMaterializationError("donor materialization destination already exists")

        fd, temp_name = tempfile.mkstemp(prefix="expected-donor.", suffix=".pending", dir=str(stage_dir))
        os.close(fd)
        temp = Path(temp_name)
        owned.append(temp)
        copied_sha, copied_size = _copy_preserved_bytes(source, temp)
        if copied_sha != rebuilt.expected_sha256 or copied_size != rebuilt.donor_size_bytes:
            raise RecoveryDonorMaterializationError("copied donor bytes do not match reviewed expected evidence")
        if sha256_file(temp) != rebuilt.expected_sha256 or int(temp.stat().st_size) != copied_size:
            raise RecoveryDonorMaterializationError("pending donor materialization failed readback verification")
        try:
            donor_after_copy = observe_stable_image(source, expected_sha256=rebuilt.expected_sha256)
        except PhysicalObservationError as exc:
            raise RecoveryDonorMaterializationError("donor changed during materialization") from exc
        if not _same_observation(donor_before, donor_after_copy):
            raise RecoveryDonorMaterializationError("donor changed during materialization")

        os.replace(temp, destination)
        owned.remove(temp)
        owned.append(destination)
        _fsync_directory(stage_dir)
        destination_identity = _regular_file_identity(destination)

        materialized_at = _now()
        manifest_payload = {
            "schema": DONOR_MANIFEST_SCHEMA,
            "materialization_id": rebuilt.materialization_id,
            "stage_id": rebuilt.stage_id,
            "proposal_id": rebuilt.proposal_id,
            "expected_revision_id": rebuilt.expected_revision_id,
            "expected_sha256": rebuilt.expected_sha256,
            "donor_file_id": rebuilt.donor_file_id,
            "donor_source_path": rebuilt.donor_path,
            "donor_source_observation": _obs_payload(donor_before),
            "donor_materialization_path": str(destination),
            "donor_materialized_sha256": rebuilt.expected_sha256,
            "donor_materialized_size_bytes": copied_size,
            "target_replacement_performed": False,
            "recovery_execution_authorized": False,
            "materialized_at": materialized_at,
        }
        manifest_sha = _write_json_manifest(manifest, manifest_payload)
        owned.append(manifest)
        manifest_identity = _regular_file_identity(manifest)

        # Reprove both the old Phase-14 checkpoint and the new donor artifact,
        # then re-attest source reality immediately before the DB checkpoint.
        row = _stage_row(conn, rebuilt.stage_id)
        _verify_committed_stage_evidence(row, stage_dir)
        if sha256_file(destination) != rebuilt.expected_sha256 or int(destination.stat().st_size) != copied_size:
            raise RecoveryDonorMaterializationError("materialized donor changed before commit")
        if sha256_file(manifest) != manifest_sha:
            raise RecoveryDonorMaterializationError("donor materialization manifest changed before commit")
        try:
            donor_final = observe_stable_image(source, expected_sha256=rebuilt.expected_sha256)
            target_final = observe_stable_image(Path(row["target_path"]), expected_sha256=rebuilt.expected_sha256)
        except PhysicalObservationError as exc:
            raise RecoveryDonorMaterializationError("source evidence changed before donor checkpoint commit") from exc
        if not _same_observation(donor_before, donor_final):
            raise RecoveryDonorMaterializationError("donor changed before donor checkpoint commit")
        if target_final.state != row["target_state"] or target_final.sha256 != row["target_observed_sha256"]:
            raise RecoveryDonorMaterializationError("target changed before donor checkpoint commit")
        if target_final.size_bytes != row["target_size_bytes"] or target_final.mtime_ns != row["target_mtime_ns"]:
            raise RecoveryDonorMaterializationError("target changed before donor checkpoint commit")

        result_evidence = {
            "plan_fingerprint": rebuilt.evidence_fingerprint,
            "donor_materialized_sha256": rebuilt.expected_sha256,
            "donor_materialized_size_bytes": copied_size,
            "donor_manifest_sha256": manifest_sha,
            "donor_final": _obs_payload(donor_final),
            "target_final": _obs_payload(target_final),
            "target_replacement_performed": False,
            "recovery_execution_authorized": False,
        }
        result_fp = _fingerprint(result_evidence)
        conn.execute(
            """
            INSERT INTO archive_recovery_donor_materializations(
                materialization_id,stage_id,proposal_id,target_file_id,donor_file_id,
                expected_revision_id,expected_sha256,recovery_intent_resolution_id,
                phase13_evidence_fingerprint,phase14_stage_fingerprint,phase14_1_plan_fingerprint,
                donor_source_path,donor_materialization_path,donor_materialized_sha256,
                donor_materialized_size_bytes,donor_manifest_path,donor_manifest_sha256,
                materialization_state,target_replacement_performed,recovery_execution_authorized,
                evidence_fingerprint,note,materialized_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rebuilt.materialization_id,rebuilt.stage_id,rebuilt.proposal_id,rebuilt.file_id,
                rebuilt.donor_file_id,rebuilt.expected_revision_id,rebuilt.expected_sha256,
                rebuilt.recovery_intent_resolution_id,rebuilt.phase13_evidence_fingerprint,
                rebuilt.phase14_stage_fingerprint,rebuilt.evidence_fingerprint,rebuilt.donor_path,
                str(destination),rebuilt.expected_sha256,copied_size,str(manifest),manifest_sha,
                MATERIALIZED,0,0,result_fp,note,materialized_at,
            ),
        )
        conn.execute(
            "INSERT INTO integrity_events(file_id,event_type,detail) VALUES (?,?,?)",
            (
                rebuilt.file_id,
                "archive_recovery_donor_materialized",
                f"Verified donor bytes for stage {rebuilt.stage_id} were materialized as SHA-256 "
                f"{rebuilt.expected_sha256} in protected operational storage. The source target was NOT replaced.",
            ),
        )
        conn.commit()
        _chmod_read_only(destination, destination_identity)
        _chmod_read_only(manifest, manifest_identity)
        return RecoveryDonorMaterializationResult(
            schema=DONOR_RESULT_SCHEMA,
            materialization_id=rebuilt.materialization_id,
            stage_id=rebuilt.stage_id,
            materialization_state=MATERIALIZED,
            donor_materialization_path=str(destination),
            donor_materialized_sha256=rebuilt.expected_sha256,
            donor_materialized_size_bytes=copied_size,
            donor_manifest_path=str(manifest),
            donor_manifest_sha256=manifest_sha,
            target_replacement_performed=False,
            recovery_execution_authorized=False,
            materialized_at=materialized_at,
            evidence_fingerprint=result_fp,
        )
    except Exception:
        try:
            conn.rollback()
        finally:
            if stage_dir is not None and stage_identity is not None:
                _cleanup_owned(owned, stage_dir, stage_identity)
        raise


def concise_donor_plan_text(plan: RecoveryDonorMaterializationPlan) -> str:
    return "\n".join([
        "Phase 14.1 — Verified Donor Materialization",
        f"Stage: {plan.stage_id}",
        f"Donor: {plan.donor_file_id}  {plan.donor_path}",
        f"Expected SHA-256: {plan.expected_sha256}",
        f"Materialization path: {plan.donor_materialization_path}",
        "Target replacement authorised: NO",
        "Recovery execution authorised: NO",
        f"Evidence fingerprint: {plan.evidence_fingerprint}",
    ])


def concise_donor_result_text(result: RecoveryDonorMaterializationResult) -> str:
    return "\n".join([
        "Phase 14.1 — Donor materialization complete",
        f"Stage: {result.stage_id}",
        f"Materialized path: {result.donor_materialization_path}",
        f"SHA-256: {result.donor_materialized_sha256}",
        "Target replacement performed: NO",
        "Recovery execution authorised: NO",
    ])
