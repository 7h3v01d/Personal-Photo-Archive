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
import stat
from pathlib import Path
import uuid
from sqlite3 import Connection

from ppa.hashing import sha256_file
from ppa.secure_write import (
    BoundDirectory, BoundTemporaryFile, SecureWriteError, atomic_write_bytes,
    descriptor_bound_directory_mutation_available, is_windows_reparse_point_stat,
    windows_path_has_reparse_component,
)
from ppa.physical_observation import PhysicalObservationError, StableFileObservation, observe_stable_image
from ppa.recovery_planning import RecoveryPlanningError, build_recovery_plan
from ppa.recovery_preservation import (
    RecoveryPreservationError,
    _attest_single_link_evidence,
    _canonical,
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
MANIFEST_FILESYSTEM = "filesystem_file"
MANIFEST_EMBEDDED = "catalogue_embedded"
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
    if windows_path_has_reparse_component(stage_dir):
        raise RecoveryDonorMaterializationError(
            "recorded recovery stage directory is unsafe because it traverses a Windows reparse point"
        )
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
    donor_manifest_storage: str
    donor_manifest_payload_json: str | None
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



def _expected_materialization_paths(row, stage_dir: Path) -> tuple[Path, Path]:
    suffix = Path(str(row["donor_path"])).suffix or ".bin"
    return stage_dir / ("expected-donor" + suffix), stage_dir / "donor-materialization.json"


def _try_bind_stage(stage_dir: Path, stage_identity: tuple[int, int]) -> BoundDirectory | None:
    """Return descriptor-bound mutation authority when the platform provides it.

    Windows deliberately returns ``None`` through ``BoundDirectory.open``.  That
    is not an error for read-only recovery logic; callers must choose a verified
    forward path rather than falling back to pathname deletion.
    """
    if not descriptor_bound_directory_mutation_available():
        return None
    try:
        return BoundDirectory.open(stage_dir, expected_identity=stage_identity)
    except SecureWriteError as exc:
        raise RecoveryDonorMaterializationError(
            "recovery stage could not be bound to its expected directory object"
        ) from exc


def _safe_operational_regular_stat(path: Path, *, label: str):
    try:
        st = path.lstat()
    except OSError as exc:
        raise RecoveryDonorMaterializationError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(st.st_mode)
        or is_windows_reparse_point_stat(st)
        or not stat.S_ISREG(st.st_mode)
    ):
        raise RecoveryDonorMaterializationError(
            f"{label} must be a regular non-reparse operational file"
        )
    return st


def _same_fs_object(a, b) -> bool:
    return (int(a.st_dev), int(a.st_ino)) == (int(b.st_dev), int(b.st_ino))


def _filesystem_identity_has_source_authority(
    conn: Connection, identity: tuple[int, int]
) -> bool:
    """Return whether an exact filesystem object is or was a registered source File.

    Phase 14.1.17.2 treats source authority as historical: moving a source object
    away from its catalogued pathname, or later observing a replacement object at
    that pathname, must not make the original object eligible for operational
    orphan adoption.  History rows cascade with their owning File when a Library
    is explicitly forgotten, so only still-registered source authority survives.
    """
    device_id, object_id = (str(identity[0]), str(identity[1]))
    current = conn.execute(
        "SELECT 1 FROM files WHERE fs_device_id=? AND fs_object_id=? LIMIT 1",
        (device_id, object_id),
    ).fetchone()
    if current is not None:
        return True
    historical = conn.execute(
        "SELECT 1 FROM file_storage_identity_history h "
        "JOIN files f ON f.id=h.file_id "
        "WHERE h.device_id=? AND h.object_id=? LIMIT 1",
        (device_id, object_id),
    ).fetchone()
    return historical is not None


def _reject_source_authority(
    conn: Connection, identity: tuple[int, int], *, label: str
) -> None:
    if _filesystem_identity_has_source_authority(conn, identity):
        raise RecoveryDonorMaterializationError(
            f"{label} is a filesystem object known to PPA as source-library evidence; "
            "automatic operational adoption is forbidden and manual intervention is required"
        )


def _orphan_manifest_payload(
    plan: RecoveryDonorMaterializationPlan,
    donor_source_observation: StableFileObservation,
    *,
    copied_size: int,
    materialized_at: str,
) -> dict:
    return {
        "schema": DONOR_MANIFEST_SCHEMA,
        "materialization_id": plan.materialization_id,
        "stage_id": plan.stage_id,
        "proposal_id": plan.proposal_id,
        "expected_revision_id": plan.expected_revision_id,
        "expected_sha256": plan.expected_sha256,
        "donor_file_id": plan.donor_file_id,
        "donor_source_path": plan.donor_path,
        "donor_source_observation": _obs_payload(donor_source_observation),
        "donor_materialization_path": plan.donor_materialization_path,
        "donor_materialized_sha256": plan.expected_sha256,
        "donor_materialized_size_bytes": copied_size,
        "target_replacement_performed": False,
        "recovery_execution_authorized": False,
        "materialized_at": materialized_at,
    }


def _canonical_manifest_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _canonical_manifest_json(payload: dict) -> tuple[str, str]:
    raw = _canonical_manifest_bytes(payload)
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def _validated_existing_orphan_manifest(
    conn: Connection,
    manifest: Path,
    *,
    row,
    destination: Path,
    expected_size: int,
) -> tuple[str, str, str, tuple[int, int]] | None:
    """Validate an already-written pre-checkpoint manifest without modifying it."""
    if not os.path.lexists(os.fspath(manifest)):
        return None
    manifest_st = _safe_operational_regular_stat(manifest, label="orphan donor manifest")
    if int(getattr(manifest_st, "st_nlink", 1) or 1) != 1:
        raise RecoveryDonorMaterializationError(
            "orphan donor manifest has multiple hard links; manual intervention required"
        )
    manifest_identity = (int(manifest_st.st_dev), int(manifest_st.st_ino))
    _reject_source_authority(conn, manifest_identity, label="orphan donor manifest")
    try:
        raw = manifest.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryDonorMaterializationError(
            "uncheckpointed donor manifest is not valid Phase-14.1 evidence; manual intervention required"
        ) from exc
    required = {
        "schema": DONOR_MANIFEST_SCHEMA,
        "stage_id": str(row["stage_id"]),
        "proposal_id": str(row["proposal_id"]),
        "expected_revision_id": str(row["expected_revision_id"]),
        "expected_sha256": str(row["expected_sha256"]),
        "donor_file_id": str(row["donor_file_id"]),
        "donor_source_path": str(row["donor_path"]),
        "donor_materialization_path": str(destination),
        "donor_materialized_sha256": str(row["expected_sha256"]),
        "donor_materialized_size_bytes": int(expected_size),
        "target_replacement_performed": False,
        "recovery_execution_authorized": False,
    }
    for key, expected in required.items():
        if payload.get(key) != expected:
            raise RecoveryDonorMaterializationError(
                "uncheckpointed donor manifest conflicts with current recovery authority; manual intervention required"
            )
    try:
        mid = _materialization_id(payload.get("materialization_id"))
    except RecoveryDonorMaterializationError as exc:
        raise RecoveryDonorMaterializationError(
            "uncheckpointed donor manifest has an invalid materialization identity; manual intervention required"
        ) from exc
    materialized_at = str(payload.get("materialized_at") or "").strip()
    if not materialized_at:
        raise RecoveryDonorMaterializationError(
            "uncheckpointed donor manifest lacks materialization time; manual intervention required"
        )
    return mid, materialized_at, hashlib.sha256(raw).hexdigest(), manifest_identity


def _adopt_verified_donor_orphan_without_delete(
    conn: Connection,
    *,
    row,
    stage_dir: Path,
    stage_identity: tuple[int, int],
) -> dict:
    """Safely move a verified orphan forward without deleting it.

    This path is the reconciliation mechanism on every platform once automatic
    POSIX child-name deletion was retired in Phase 14.1.17.  It never unlinks
    operational children.  It accepts only the one final
    expected-donor artifact, rejects temporary debris and reparse/hard-link
    aliases, and freshly re-proves source and target authority.  A pre-existing
    valid filesystem manifest may be verified read-only; when the manifest is
    missing, its canonical bytes are embedded in the append-only catalogue rather
    than written through an unbound stage pathname.  Invalid or ambiguous debris
    is left untouched for explicit manual intervention.
    """
    if _directory_identity(stage_dir) != stage_identity:
        raise RecoveryDonorMaterializationError(
            "recovery stage changed before orphan adoption; operational debris was left untouched"
        )
    _verify_committed_stage_evidence(row, stage_dir)
    destination, manifest = _expected_materialization_paths(row, stage_dir)

    try:
        names = {child.name for child in stage_dir.iterdir()}
    except OSError as exc:
        raise RecoveryDonorMaterializationError(
            "cannot inspect uncheckpointed donor artifacts safely; manual intervention required"
        ) from exc
    if _directory_identity(stage_dir) != stage_identity:
        raise RecoveryDonorMaterializationError(
            "recovery stage changed while orphan artifacts were inspected; operational debris was left untouched"
        )

    temporary_names = sorted(
        name for name in names
        if (name.startswith("expected-donor.") and name.endswith(".pending"))
        or (name.startswith("donor-manifest.") and name.endswith(".tmp"))
    )
    if temporary_names:
        raise RecoveryDonorMaterializationError(
            "uncheckpointed temporary donor artifacts require manual intervention on this platform; "
            "no source or operational file was modified"
        )
    if destination.name not in names:
        if manifest.name in names:
            raise RecoveryDonorMaterializationError(
                "orphan donor manifest exists without its verified donor artifact; manual intervention required"
            )
        return {
            "stage_id": str(row["stage_id"]),
            "state": "clean",
            "removed": [],
            "adopted": [],
        }

    orphan_st = _safe_operational_regular_stat(destination, label="orphan donor materialization")
    if int(getattr(orphan_st, "st_nlink", 1) or 1) != 1:
        raise RecoveryDonorMaterializationError(
            "orphan donor materialization has multiple hard links; refusing adoption"
        )
    orphan_identity = (int(orphan_st.st_dev), int(orphan_st.st_ino))
    _reject_source_authority(
        conn, orphan_identity, label="orphan donor materialization"
    )

    # Existing source paths must not be the same filesystem object as the orphan.
    for source_label, source_path in (
        ("trusted donor", Path(str(row["donor_path"]))),
        ("suspect target", Path(str(row["target_path"]))),
    ):
        if not os.path.lexists(os.fspath(source_path)):
            continue
        try:
            source_st = source_path.lstat()
        except OSError as exc:
            raise RecoveryDonorMaterializationError(
                f"cannot verify {source_label} filesystem identity during orphan adoption"
            ) from exc
        if (
            stat.S_ISLNK(source_st.st_mode)
            or is_windows_reparse_point_stat(source_st)
            or not stat.S_ISREG(source_st.st_mode)
        ):
            raise RecoveryDonorMaterializationError(
                f"{source_label} is not a safe regular file during orphan adoption"
            )
        if _same_fs_object(orphan_st, source_st):
            raise RecoveryDonorMaterializationError(
                f"orphan donor materialization aliases the {source_label}; refusing adoption"
            )

    manifest_info = _validated_existing_orphan_manifest(
        conn,
        manifest,
        row=row,
        destination=destination,
        expected_size=int(row["donor_size_bytes"]),
    )
    materialization_id = manifest_info[0] if manifest_info else _materialization_id(None)
    plan = _build_donor_materialization_plan(
        conn,
        stage_id=str(row["stage_id"]),
        materialization_id=materialization_id,
        allow_uncheckpointed_artifacts=True,
    )
    if Path(plan.donor_materialization_path) != destination or Path(plan.donor_manifest_path) != manifest:
        raise RecoveryDonorMaterializationError("orphan donor paths do not match rebuilt recovery authority")

    try:
        orphan_obs = observe_stable_image(destination, expected_sha256=plan.expected_sha256)
        donor_obs = observe_stable_image(Path(plan.donor_path), expected_sha256=plan.expected_sha256)
        target_obs = observe_stable_image(Path(row["target_path"]), expected_sha256=plan.expected_sha256)
    except PhysicalObservationError as exc:
        raise RecoveryDonorMaterializationError(
            "physical recovery evidence changed while orphan adoption was verified"
        ) from exc
    if orphan_obs.state != "matches_expected" or orphan_obs.sha256 != plan.expected_sha256:
        raise RecoveryDonorMaterializationError(
            "orphan donor artifact does not reproduce the immutable expected revision"
        )
    if int(orphan_obs.size_bytes or 0) != int(plan.donor_size_bytes):
        raise RecoveryDonorMaterializationError("orphan donor artifact size does not match expected donor evidence")
    if donor_obs.state != "matches_expected" or donor_obs.sha256 != plan.expected_sha256:
        raise RecoveryDonorMaterializationError("trusted donor changed before orphan adoption")
    if target_obs.state != row["target_state"] or target_obs.sha256 != row["target_observed_sha256"]:
        raise RecoveryDonorMaterializationError("target changed before orphan adoption")
    if target_obs.size_bytes != row["target_size_bytes"] or target_obs.mtime_ns != row["target_mtime_ns"]:
        raise RecoveryDonorMaterializationError("target physical evidence changed before orphan adoption")
    if _regular_file_identity(destination) != orphan_identity:
        raise RecoveryDonorMaterializationError("orphan donor filesystem object changed during adoption")
    if _directory_identity(stage_dir) != stage_identity:
        raise RecoveryDonorMaterializationError("recovery stage changed during orphan adoption")

    materialized_at = manifest_info[1] if manifest_info else _now()
    manifest_payload_json: str | None = None
    if manifest_info is None:
        # A missing orphan manifest is never recreated through a validated
        # pathname: that would reintroduce namespace mutation after an identity
        # check.  Preserve the canonical bytes in the append-only catalogue.
        # Preserve the exact canonical manifest bytes in the append-only catalogue
        # instead.  This is evidence, not filesystem mutation.
        manifest_payload = _orphan_manifest_payload(
            plan, donor_obs, copied_size=int(orphan_obs.size_bytes or 0), materialized_at=materialized_at
        )
        manifest_payload_json, manifest_sha = _canonical_manifest_json(manifest_payload)
        manifest_storage = MANIFEST_EMBEDDED
    else:
        manifest_sha = manifest_info[2]
        manifest_storage = MANIFEST_FILESYSTEM

    # Re-prove the exact stage, orphan, source donor and target immediately before
    # the immutable checkpoint is appended.  No filesystem write occurs in the
    # missing-manifest Windows adoption path.
    if _directory_identity(stage_dir) != stage_identity:
        raise RecoveryDonorMaterializationError("recovery stage changed before orphan checkpoint commit")
    if _regular_file_identity(destination) != orphan_identity:
        raise RecoveryDonorMaterializationError("orphan donor changed before checkpoint commit")
    if manifest_storage == MANIFEST_FILESYSTEM:
        manifest_identity = _regular_file_identity(manifest)
        if manifest_identity != manifest_info[3]:
            raise RecoveryDonorMaterializationError(
                "orphan donor manifest filesystem object changed before checkpoint commit"
            )
        if sha256_file(manifest) != manifest_sha:
            raise RecoveryDonorMaterializationError("orphan donor manifest changed before checkpoint commit")
    else:
        manifest_identity = None
        if os.path.lexists(os.fspath(manifest)):
            raise RecoveryDonorMaterializationError(
                "unexpected filesystem donor manifest appeared during embedded orphan adoption"
            )
    try:
        donor_final = observe_stable_image(Path(plan.donor_path), expected_sha256=plan.expected_sha256)
        target_final = observe_stable_image(Path(row["target_path"]), expected_sha256=plan.expected_sha256)
        orphan_final = observe_stable_image(destination, expected_sha256=plan.expected_sha256)
    except PhysicalObservationError as exc:
        raise RecoveryDonorMaterializationError("recovery evidence changed before orphan checkpoint commit") from exc
    if not _same_observation(donor_obs, donor_final):
        raise RecoveryDonorMaterializationError("trusted donor changed before orphan checkpoint commit")
    if target_final.state != row["target_state"] or target_final.sha256 != row["target_observed_sha256"]:
        raise RecoveryDonorMaterializationError("target changed before orphan checkpoint commit")
    if orphan_final.state != "matches_expected" or orphan_final.sha256 != plan.expected_sha256:
        raise RecoveryDonorMaterializationError("orphan donor changed before checkpoint commit")

    try:
        _attest_single_link_evidence(
            destination,
            expected_identity=orphan_identity,
            expected_sha256=plan.expected_sha256,
            expected_size=int(orphan_final.size_bytes or 0),
            label="orphan donor materialization",
        )
        if manifest_identity is not None:
            _attest_single_link_evidence(
                manifest,
                expected_identity=manifest_identity,
                expected_sha256=manifest_sha,
                label="orphan donor manifest",
            )
    except RecoveryPreservationError as exc:
        raise RecoveryDonorMaterializationError(str(exc)) from exc

    result_evidence = {
        "plan_fingerprint": plan.evidence_fingerprint,
        "donor_materialized_sha256": plan.expected_sha256,
        "donor_materialized_size_bytes": int(orphan_final.size_bytes or 0),
        "donor_manifest_sha256": manifest_sha,
        "donor_manifest_storage": manifest_storage,
        "donor_final": _obs_payload(donor_final),
        "target_final": _obs_payload(target_final),
        "target_replacement_performed": False,
        "recovery_execution_authorized": False,
        "orphan_adopted": True,
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
            donor_manifest_storage,donor_manifest_payload_json,
            materialization_state,target_replacement_performed,recovery_execution_authorized,
            evidence_fingerprint,note,materialized_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            plan.materialization_id,plan.stage_id,plan.proposal_id,plan.file_id,
            plan.donor_file_id,plan.expected_revision_id,plan.expected_sha256,
            plan.recovery_intent_resolution_id,plan.phase13_evidence_fingerprint,
            plan.phase14_stage_fingerprint,plan.evidence_fingerprint,plan.donor_path,
            str(destination),plan.expected_sha256,int(orphan_final.size_bytes or 0),
            str(manifest),manifest_sha,manifest_storage,manifest_payload_json,MATERIALIZED,0,0,result_fp,
            "Verified orphan donor artifact adopted without destructive cleanup; "
            + ("canonical manifest embedded in catalogue" if manifest_storage == MANIFEST_EMBEDDED else "existing filesystem manifest verified"),
            materialized_at,
        ),
    )
    conn.execute(
        "INSERT INTO integrity_events(file_id,event_type,detail) VALUES (?,?,?)",
        (
            plan.file_id,
            "archive_recovery_donor_orphan_adopted",
            f"Verified uncheckpointed Phase-14.1 donor artifact for stage {plan.stage_id} "
            "was adopted into immutable recovery evidence without deleting operational or source files.",
        ),
    )
    conn.commit()
    # Evidence finalisation ends at the immutable catalogue checkpoint.  No
    # post-commit chmod or other filesystem metadata mutation is performed.
    return {
        "stage_id": plan.stage_id,
        "state": "orphan_artifact_adopted",
        "removed": [],
        "adopted": [destination.name] + ([manifest.name] if manifest_storage == MANIFEST_FILESYSTEM else []),
        "manifest_storage": manifest_storage,
        "materialization_id": plan.materialization_id,
    }


def reconcile_donor_materialization_orphans(conn: Connection, *, stage_id: str) -> dict:
    """Reconcile uncheckpointed donor artifacts without POSIX name deletion.

    Phase 14.1.17 applies exact destructive-child authority consistently.  A
    bound POSIX directory descriptor proves the parent namespace, but it cannot
    make ``stat(child) -> unlink(child)`` atomic with respect to substitution.
    Reconciliation therefore uses only the safe forward path: adopt a freshly
    verified final donor artifact into immutable evidence.  Temporary, invalid,
    or ambiguous debris is retained for manual intervention.
    """
    sid = _validated_stage_id(stage_id)
    bound_stage: BoundDirectory | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _stage_row(conn, sid)
        if row is None:
            raise RecoveryDonorMaterializationError(
                "unknown committed recovery preservation stage"
            )

        existing = conn.execute(
            "SELECT materialization_id FROM archive_recovery_donor_materializations "
            "WHERE stage_id=?",
            (sid,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return {"stage_id": sid, "state": "checkpoint_exists", "removed": []}

        _root, stage_dir = _stage_dir_from_row(conn, row)
        stage_identity = _directory_identity(stage_dir)
        bound_stage = _try_bind_stage(stage_dir, stage_identity)
        if bound_stage is not None:
            # Bind/re-prove the committed stage object so even read-only orphan
            # inspection cannot silently drift to a substituted stage pathname.
            if not bound_stage.pathname_still_bound():
                raise RecoveryDonorMaterializationError(
                    "committed recovery stage pathname no longer identifies its bound directory"
                )
            _verify_committed_stage_evidence(row, stage_dir)

        # POSIX and unsupported platforms now share the same non-destructive
        # reconciliation rule.  A valid final orphan can move forward; anything
        # else remains untouched.  No child name is automatically unlinked.
        return _adopt_verified_donor_orphan_without_delete(
            conn, row=row, stage_dir=stage_dir, stage_identity=stage_identity
        )
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if bound_stage is not None:
            bound_stage.close()


def _build_donor_materialization_plan(
    conn: Connection,
    *,
    stage_id: str,
    materialization_id: str | None = None,
    allow_uncheckpointed_artifacts: bool = False,
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
    donor_path, donor_manifest = _expected_materialization_paths(row, stage_dir)
    if not allow_uncheckpointed_artifacts:
        uncheckpointed = (
            donor_path.exists() or donor_path.is_symlink()
            or donor_manifest.exists() or donor_manifest.is_symlink()
        )
        if not uncheckpointed:
            try:
                uncheckpointed = any(
                    (child.name.startswith("expected-donor.") and child.name.endswith(".pending"))
                    or (child.name.startswith("donor-manifest.") and child.name.endswith(".tmp"))
                    for child in stage_dir.iterdir()
                )
            except OSError as exc:
                raise RecoveryDonorMaterializationError(
                    "cannot inspect donor stage for uncheckpointed artifacts"
                ) from exc
        if uncheckpointed:
            raise RecoveryDonorMaterializationError(
                "uncheckpointed donor materialization artifact exists; run donor-orphan reconciliation"
            )
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


def build_donor_materialization_plan(
    conn: Connection,
    *,
    stage_id: str,
    materialization_id: str | None = None,
) -> RecoveryDonorMaterializationPlan:
    return _build_donor_materialization_plan(
        conn,
        stage_id=stage_id,
        materialization_id=materialization_id,
        allow_uncheckpointed_artifacts=False,
    )


def _write_json_manifest(
    path: Path, payload: dict, expected_parent_identity: tuple[int, int] | None = None
) -> str:
    raw = _canonical_manifest_bytes(payload)
    try:
        atomic_write_bytes(
            path, raw, prefix="donor-manifest.", suffix=".tmp", replace=True,
            expected_parent_identity=expected_parent_identity,
        )
    except SecureWriteError as exc:
        raise RecoveryDonorMaterializationError("donor manifest temporary identity changed") from exc
    return hashlib.sha256(raw).hexdigest()


def _cleanup_owned(
    owned_paths: list[Path],
    bound_stage: BoundDirectory | None,
) -> None:
    """Retain uncheckpointed POSIX stage children as operational debris.

    The entries are known to have originated from this operation, but POSIX
    supplies no general exact-object unlink primitive.  A child can be renamed
    away and a source object inserted under the same name after any identity
    check.  Rollback therefore performs no automatic child deletion.
    """
    if bound_stage is None:
        return
    bound_stage.verify_handle()
    for path in owned_paths:
        try:
            if path.parent == bound_stage.path:
                BoundDirectory.validate_child_name(path.name)
        except SecureWriteError:
            continue
    # Intentionally no unlink: retained debris is safer than destructive
    # check-then-delete authority.


def _donor_checkpoint_is_durable(
    conn: Connection, materialization_id: str | None
) -> bool:
    """Return whether the donor checkpoint is already committed.

    Never treat a row visible inside the connection's own active transaction as
    durable.  If SQLite reports no active transaction, a visible immutable
    checkpoint is authoritative and exception cleanup must not delete the files
    it records.  Query ambiguity after the transaction ended fails closed toward
    evidence preservation.
    """
    if materialization_id is None or conn.in_transaction:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM archive_recovery_donor_materializations "
            "WHERE materialization_id=? LIMIT 1",
            (materialization_id,),
        ).fetchone() is not None
    except Exception:
        return True


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
    bound_stage: BoundDirectory | None = None
    checkpoint_materialization_id: str | None = None
    checkpoint_committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        rebuilt = build_donor_materialization_plan(
            conn, stage_id=plan.stage_id, materialization_id=plan.materialization_id
        )
        if rebuilt.evidence_fingerprint != plan.evidence_fingerprint:
            raise RecoveryDonorMaterializationError("donor materialization plan is stale; rebuild it")
        checkpoint_materialization_id = rebuilt.materialization_id
        stage_dir = Path(rebuilt.stage_dir)
        stage_identity = _directory_identity(stage_dir)
        # On Windows this returns None by design.  Materialization itself remains
        # descriptor-bound at the file level; if an interruption strands the final
        # donor artifact, reconciliation can now verify-and-adopt it rather than
        # requiring unsafe pathname deletion.
        bound_stage = _try_bind_stage(stage_dir, stage_identity)

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

        try:
            temp = BoundTemporaryFile.create(
                stage_dir, prefix="expected-donor.", suffix=".pending",
                expected_parent_identity=stage_identity,
            )
        except SecureWriteError as exc:
            raise RecoveryDonorMaterializationError(
                "donor stage write authority changed before temporary creation"
            ) from exc
        try:
            try:
                copied_sha, copied_size = _copy_preserved_bytes(source, temp)
            except SecureWriteError as exc:
                raise RecoveryDonorMaterializationError(
                    "donor temporary identity changed while verified bytes were copied"
                ) from exc
            if copied_sha != rebuilt.expected_sha256 or copied_size != rebuilt.donor_size_bytes:
                raise RecoveryDonorMaterializationError("copied donor bytes do not match reviewed expected evidence")
            readback_sha, readback_size = temp.hash_and_size()
            if readback_sha != rebuilt.expected_sha256 or readback_size != copied_size:
                raise RecoveryDonorMaterializationError("pending donor materialization failed readback verification")
            try:
                donor_after_copy = observe_stable_image(source, expected_sha256=rebuilt.expected_sha256)
            except PhysicalObservationError as exc:
                raise RecoveryDonorMaterializationError("donor changed during materialization") from exc
            if not _same_observation(donor_before, donor_after_copy):
                raise RecoveryDonorMaterializationError("donor changed during materialization")

            try:
                temp.install(destination, replace=False)
            except SecureWriteError as exc:
                raise RecoveryDonorMaterializationError(
                    "donor temporary identity/path changed before installation"
                ) from exc
        finally:
            temp.cleanup()
        owned.append(destination)
        destination_identity = _regular_file_identity(destination)

        materialized_at = _now()
        manifest_payload = _orphan_manifest_payload(
            rebuilt,
            donor_before,
            copied_size=copied_size,
            materialized_at=materialized_at,
        )
        manifest_sha = _write_json_manifest(
            manifest, manifest_payload, stage_identity
        )
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

        try:
            _attest_single_link_evidence(
                destination,
                expected_identity=destination_identity,
                expected_sha256=rebuilt.expected_sha256,
                expected_size=copied_size,
                label="donor materialization",
            )
            _attest_single_link_evidence(
                manifest,
                expected_identity=manifest_identity,
                expected_sha256=manifest_sha,
                label="donor materialization manifest",
            )
        except RecoveryPreservationError as exc:
            raise RecoveryDonorMaterializationError(str(exc)) from exc

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
                donor_manifest_storage,donor_manifest_payload_json,
                materialization_state,target_replacement_performed,recovery_execution_authorized,
                evidence_fingerprint,note,materialized_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rebuilt.materialization_id,rebuilt.stage_id,rebuilt.proposal_id,rebuilt.file_id,
                rebuilt.donor_file_id,rebuilt.expected_revision_id,rebuilt.expected_sha256,
                rebuilt.recovery_intent_resolution_id,rebuilt.phase13_evidence_fingerprint,
                rebuilt.phase14_stage_fingerprint,rebuilt.evidence_fingerprint,rebuilt.donor_path,
                str(destination),rebuilt.expected_sha256,copied_size,str(manifest),manifest_sha,
                MANIFEST_FILESYSTEM,None,MATERIALIZED,0,0,result_fp,note,materialized_at,
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
        checkpoint_committed = True
        # Phase 14.1.17.4: no post-commit evidence mutation.
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
            donor_manifest_storage=MANIFEST_FILESYSTEM,
            donor_manifest_payload_json=None,
            target_replacement_performed=False,
            recovery_execution_authorized=False,
            materialized_at=materialized_at,
            evidence_fingerprint=result_fp,
        )
    except BaseException:
        durable = checkpoint_committed or _donor_checkpoint_is_durable(
            conn, checkpoint_materialization_id
        )
        if not durable:
            try:
                if conn.in_transaction:
                    conn.rollback()
            finally:
                _cleanup_owned(owned, bound_stage)
        raise
    finally:
        if bound_stage is not None:
            bound_stage.close()


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
