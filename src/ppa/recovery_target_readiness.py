"""Phase 14.2 — target-replacement readiness protocol.

This phase is deliberately planning/audit only.  It proves whether the exact
Phase-14 preservation + donor-materialization evidence chain is still physically
coherent enough to be reviewed for a *future* target-replacement protocol.
It never creates, replaces, renames, deletes, chmods, or otherwise mutates a
source photograph and it grants no target-write or recovery-execution authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import stat
from pathlib import Path
from sqlite3 import Connection
import uuid

from ppa.mismatch_resolution import ACTION_RETAIN_EXPECTED, latest_mismatch_resolution
from ppa.physical_observation import PhysicalObservationError, StableFileObservation, observe_stable_image
from ppa.recovery_donor_materialization import MANIFEST_EMBEDDED, MANIFEST_FILESYSTEM
from ppa.recovery_preservation import RecoveryPreservationError, _attest_single_link_evidence
from ppa.secure_write import (
    SecureWriteError,
    bind_directory_authority,
    is_windows_reparse_point_stat,
    windows_path_has_reparse_component,
)

READINESS_SCHEMA = "ppa-recovery-target-replacement-readiness/1"
READINESS_STATE = "ready_for_replacement_protocol_review"
MODE_REPLACE = "replace_existing_exact_target"
MODE_RESTORE = "restore_missing_recorded_target"


class RecoveryTargetReadinessError(ValueError):
    """The frozen recovery chain is not currently ready for replacement review."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _validated_uuid(value: str | None, *, label: str) -> str:
    if value is None:
        return str(uuid.uuid4())
    raw = str(value).strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise RecoveryTargetReadinessError(f"{label} must be a canonical UUID") from exc
    if raw != str(parsed):
        raise RecoveryTargetReadinessError(f"{label} must be a canonical UUID")
    return raw


def _row(conn: Connection, table: str, column: str, value: str):
    # table/column are module constants only; never caller input.
    return conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,)).fetchone()


def _target_observation_matches_stage(stage, obs: StableFileObservation) -> bool:
    expected = (
        str(stage["target_state"]),
        stage["target_observed_sha256"],
        stage["target_size_bytes"],
        stage["target_mtime_ns"],
        None if stage["target_fs_device_id"] is None else str(stage["target_fs_device_id"]),
        None if stage["target_fs_object_id"] is None else str(stage["target_fs_object_id"]),
    )
    actual = (
        obs.state,
        obs.sha256,
        obs.size_bytes,
        obs.mtime_ns,
        None if obs.fs_device_id is None else str(obs.fs_device_id),
        None if obs.fs_object_id is None else str(obs.fs_object_id),
    )
    return expected == actual


def _safe_regular_current_identity(path: Path, *, label: str) -> tuple[int, int]:
    if windows_path_has_reparse_component(path):
        raise RecoveryTargetReadinessError(f"{label} traverses a Windows reparse point")
    try:
        st = path.lstat()
    except OSError as exc:
        raise RecoveryTargetReadinessError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(st.st_mode) or is_windows_reparse_point_stat(st) or not stat.S_ISREG(st.st_mode):
        raise RecoveryTargetReadinessError(f"{label} is not a safe regular file")
    return int(st.st_dev), int(st.st_ino)




def _observe_destination_target(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[StableFileObservation, int | None]:
    """Build one identity-bound, read-only target snapshot.

    ``observe_stable_image`` proves stable content/size/mtime/device/object state.
    For a present target, the subsequent pathname metadata observation used for
    link topology must describe that *same exact object* and the same size/mtime.
    This prevents the link-count check from being accidentally borrowed from a
    replacement object that arrived after the content observation.
    """
    try:
        observed = observe_stable_image(path, expected_sha256=expected_sha256)
    except PhysicalObservationError as exc:
        raise RecoveryTargetReadinessError("target changed during readiness observation") from exc

    if observed.state == "missing":
        # Re-establish absence after the observation.  A target appearing in the
        # small interval is a changed destination snapshot, not restore readiness.
        try:
            if path.exists() or path.is_symlink():
                raise RecoveryTargetReadinessError("target appeared during readiness observation")
        except OSError as exc:
            raise RecoveryTargetReadinessError("target pathname became unavailable during readiness observation") from exc
        return observed, None

    if windows_path_has_reparse_component(path):
        raise RecoveryTargetReadinessError("target source object traverses a Windows reparse point")
    try:
        st = path.lstat()
    except OSError as exc:
        raise RecoveryTargetReadinessError("target disappeared during readiness observation") from exc
    if stat.S_ISLNK(st.st_mode) or is_windows_reparse_point_stat(st) or not stat.S_ISREG(st.st_mode):
        raise RecoveryTargetReadinessError("target source object is not a safe regular file")

    metadata_identity = (int(st.st_dev), int(st.st_ino))
    observed_identity = (
        int(observed.fs_device_id or -1),
        int(observed.fs_object_id or -1),
    )
    if metadata_identity != observed_identity:
        raise RecoveryTargetReadinessError("target filesystem identity changed during readiness observation")
    if observed.size_bytes is None or int(st.st_size) != int(observed.size_bytes):
        raise RecoveryTargetReadinessError("target size changed during readiness observation")
    if observed.mtime_ns is None or int(st.st_mtime_ns) != int(observed.mtime_ns):
        raise RecoveryTargetReadinessError("target mtime changed during readiness observation")

    nlink = int(getattr(st, "st_nlink", 1) or 1)
    if nlink != 1:
        raise RecoveryTargetReadinessError(
            "target source object has hard-link aliases; replacement topology requires explicit manual review"
        )
    return observed, nlink


def _destination_snapshot_equal(
    initial_target: StableFileObservation,
    initial_nlink: int | None,
    final_target: StableFileObservation,
    final_nlink: int | None,
) -> bool:
    return (
        initial_target.state,
        initial_target.sha256,
        initial_target.size_bytes,
        initial_target.mtime_ns,
        initial_target.fs_device_id,
        initial_target.fs_object_id,
        initial_nlink,
    ) == (
        final_target.state,
        final_target.sha256,
        final_target.size_bytes,
        final_target.mtime_ns,
        final_target.fs_device_id,
        final_target.fs_object_id,
        final_nlink,
    )


def _assert_destination_snapshot_still_current(
    conn: Connection,
    *,
    library_id: int,
    target_path: Path,
    expected_sha256: str,
    initial_target: StableFileObservation,
    initial_target_nlink: int | None,
    initial_root_path: str,
    initial_root_identity: tuple[int, int],
    initial_parent_path: str,
    initial_parent_identity: tuple[int, int],
) -> tuple[StableFileObservation, int | None, str, tuple[int, int], str, tuple[int, int]]:
    """Final destination attestation with root/parent objects pinned across hashing.

    Phase 14.2.3 closes the intra-final-attestation window by binding the
    exact registered Library root and exact target-parent directory objects
    *before* the final target stable-content observation, retaining those
    read-only identity pins while the target is hashed, and verifying that
    both original pathnames still name the bound objects immediately before
    readiness fingerprint construction.

    The bound authorities are observational here: no child mutation API is
    invoked and Phase 14.2 retains zero target-write/execution authority.
    """
    # Re-establish the persisted/root + known-parent policy immediately before
    # binding. A substitution between this check and bind is caught by the
    # bound identity comparisons below.
    final_root_path, final_root_identity, final_parent_path, final_parent_identity = _verify_source_parent_authority(
        conn, library_id=library_id, target_path=target_path
    )
    if final_root_path != initial_root_path or final_root_identity != initial_root_identity:
        raise RecoveryTargetReadinessError(
            "registered Library root filesystem identity changed during readiness evidence attestation; rescan/review required"
        )
    if final_parent_path != initial_parent_path or final_parent_identity != initial_parent_identity:
        raise RecoveryTargetReadinessError(
            "target parent directory changed during readiness evidence attestation"
        )

    root_authority = None
    parent_authority = None
    try:
        root_authority = bind_directory_authority(Path(final_root_path))
        if tuple(root_authority.identity) != tuple(initial_root_identity):
            raise RecoveryTargetReadinessError(
                "registered Library root filesystem identity changed during final topology binding; rescan/review required"
            )

        parent_authority = bind_directory_authority(Path(final_parent_path))
        if tuple(parent_authority.identity) != tuple(initial_parent_identity):
            raise RecoveryTargetReadinessError(
                "target parent directory changed during final topology binding"
            )

        # Both exact directory objects remain pinned across the potentially
        # lengthy final target observation/hash.
        final_target, final_nlink = _observe_destination_target(
            target_path, expected_sha256=expected_sha256
        )

        # Re-prove the lexical namespace still reaches the exact pinned objects
        # after target hashing. This is freshness only; the handles/descriptors
        # themselves supplied the identity pins throughout the observation.
        root_authority.verify_pathname()
        parent_authority.verify_pathname()

        if not _destination_snapshot_equal(
            initial_target, initial_target_nlink, final_target, final_nlink
        ):
            raise RecoveryTargetReadinessError(
                "target destination changed during readiness evidence attestation"
            )

        return (
            final_target, final_nlink, final_root_path, final_root_identity,
            final_parent_path, final_parent_identity,
        )
    except SecureWriteError as exc:
        raise RecoveryTargetReadinessError(
            "bound destination topology changed during final target observation; rescan/review required"
        ) from exc
    finally:
        if parent_authority is not None:
            parent_authority.close()
        if root_authority is not None:
            root_authority.close()

def _attest_operational_file(
    conn: Connection,
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int | None,
    label: str,
) -> tuple[int, int]:
    """Read-only final attestation of current operational evidence.

    The exact current object is pinned by identity between lstat and descriptor
    open and must remain single-link while it is hashed.  Source-object exclusion
    is enforced at the Phase-14 creation/adoption checkpoint; repeating historical
    inode exclusion here would create false positives when POSIX later reuses an
    inode number for a legitimate committed operational file.
    """
    identity = _safe_regular_current_identity(path, label=label)
    try:
        return _attest_single_link_evidence(
            path,
            expected_identity=identity,
            expected_sha256=str(expected_sha256),
            expected_size=expected_size,
            label=label,
        )
    except RecoveryPreservationError as exc:
        raise RecoveryTargetReadinessError(str(exc)) from exc


def _verify_source_parent_authority(
    conn: Connection,
    *,
    library_id: int,
    target_path: Path,
) -> tuple[str, tuple[int, int], str, tuple[int, int]]:
    """Prove the registered Library root and current target parent topology.

    Phase 14.2.2 extends the existing immediate-parent proof with the exact
    persisted Library-root filesystem identity.  This remains read-only
    readiness evidence, not source namespace mutation authority.
    """
    lib = conn.execute(
        "SELECT root_canonical_path,root_fs_device_id,root_fs_object_id,"
        "source_tree_identity_complete,source_tree_identity_verified_at "
        "FROM libraries WHERE id=?",
        (int(library_id),),
    ).fetchone()
    if lib is None:
        raise RecoveryTargetReadinessError("target Library is no longer registered")
    if (
        lib["root_fs_device_id"] is None
        or lib["root_fs_object_id"] is None
        or int(lib["source_tree_identity_complete"] or 0) != 1
        or not lib["source_tree_identity_verified_at"]
    ):
        raise RecoveryTargetReadinessError(
            "target Library filesystem/source-tree authority is not completely verified; rescan first"
        )

    root = Path(str(lib["root_canonical_path"]))
    if windows_path_has_reparse_component(root):
        raise RecoveryTargetReadinessError("registered Library root traverses a Windows reparse point")
    try:
        root_st = root.lstat()
    except OSError as exc:
        raise RecoveryTargetReadinessError("registered Library root directory is unavailable") from exc
    if (
        stat.S_ISLNK(root_st.st_mode)
        or is_windows_reparse_point_stat(root_st)
        or not stat.S_ISDIR(root_st.st_mode)
    ):
        raise RecoveryTargetReadinessError("registered Library root is not a safe directory object")
    root_identity = (int(root_st.st_dev), int(root_st.st_ino))
    persisted_root_identity = (
        int(lib["root_fs_device_id"]),
        int(lib["root_fs_object_id"]),
    )
    if root_identity != persisted_root_identity:
        raise RecoveryTargetReadinessError(
            "registered Library root filesystem identity changed; rescan/review required"
        )
    root_canonical = _canonical(root)

    parent = target_path.parent
    if windows_path_has_reparse_component(parent):
        raise RecoveryTargetReadinessError("target parent traverses a Windows reparse point")
    try:
        st = parent.lstat()
    except OSError as exc:
        raise RecoveryTargetReadinessError("recorded target parent directory is unavailable") from exc
    if stat.S_ISLNK(st.st_mode) or is_windows_reparse_point_stat(st) or not stat.S_ISDIR(st.st_mode):
        raise RecoveryTargetReadinessError("recorded target parent is not a safe directory object")
    identity = (int(st.st_dev), int(st.st_ino))

    target_canonical = _canonical(target_path)
    try:
        if os.path.commonpath([target_canonical, root_canonical]) != root_canonical:
            raise RecoveryTargetReadinessError("recorded target path is no longer inside its registered Library")
    except ValueError as exc:
        raise RecoveryTargetReadinessError("recorded target path is on an incompatible filesystem root") from exc

    known = conn.execute(
        "SELECT 1 FROM library_directory_identities "
        "WHERE library_id=? AND fs_device_id=? AND fs_object_id=? LIMIT 1",
        (int(library_id), str(identity[0]), str(identity[1])),
    ).fetchone()
    if known is None:
        raise RecoveryTargetReadinessError(
            "current target parent directory object is not in the verified Library source-tree inventory; rescan first"
        )
    return root_canonical, root_identity, _canonical(parent), identity


@dataclass(frozen=True)
class RecoveryTargetReadiness:
    schema: str
    readiness_id: str
    readiness_state: str
    materialization_id: str
    stage_id: str
    proposal_id: str
    file_id: str
    library_id: int
    expected_revision_id: str
    expected_sha256: str
    recovery_intent_resolution_id: str
    target_path: str
    target_state: str
    target_observed_sha256: str | None
    target_size_bytes: int | None
    target_mtime_ns: int | None
    target_fs_device_id: str | None
    target_fs_object_id: str | None
    target_link_count: int | None
    library_root_path: str
    library_root_fs_device_id: str
    library_root_fs_object_id: str
    target_parent_path: str
    target_parent_fs_device_id: str
    target_parent_fs_object_id: str
    replacement_mode: str
    preservation_path: str | None
    preservation_sha256: str | None
    preservation_manifest_path: str
    preservation_manifest_sha256: str
    donor_materialization_path: str
    donor_materialized_sha256: str
    donor_materialized_size_bytes: int
    donor_manifest_storage: str
    donor_manifest_path: str
    donor_manifest_sha256: str
    target_replacement_authorized: bool
    recovery_execution_authorized: bool
    evidence_fingerprint: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )


@dataclass(frozen=True)
class RecordedTargetReadiness:
    readiness_id: str
    materialization_id: str
    file_id: str
    evidence_fingerprint: str
    assessed_at: str


def build_target_replacement_readiness(
    conn: Connection,
    *,
    materialization_id: str,
    readiness_id: str | None = None,
) -> RecoveryTargetReadiness:
    """Build a fresh read-only Phase-14.2 readiness snapshot.

    This function never writes the filesystem or database.  It intentionally
    does not claim that replacement is authorised; it merely proves that the
    currently observed evidence chain is coherent enough for a later separately
    reviewed execution protocol to be designed around it.
    """
    mid = _validated_uuid(materialization_id, label="materialization ID")
    rid = _validated_uuid(readiness_id, label="readiness ID")

    mat = _row(conn, "archive_recovery_donor_materializations", "materialization_id", mid)
    if mat is None:
        raise RecoveryTargetReadinessError("unknown committed donor materialization")
    if mat["materialization_state"] != "verified_donor_materialized":
        raise RecoveryTargetReadinessError("donor materialization is not a verified committed checkpoint")
    if int(mat["target_replacement_performed"] or 0) != 0 or int(mat["recovery_execution_authorized"] or 0) != 0:
        raise RecoveryTargetReadinessError("donor checkpoint contains forbidden target-write authority")

    stage = _row(conn, "archive_recovery_preservation_stages", "stage_id", str(mat["stage_id"]))
    proposal = _row(conn, "archive_recovery_plan_proposals", "proposal_id", str(mat["proposal_id"]))
    if stage is None or proposal is None:
        raise RecoveryTargetReadinessError("recovery evidence chain is incomplete")
    if str(stage["evidence_fingerprint"]) != str(mat["phase14_stage_fingerprint"]):
        raise RecoveryTargetReadinessError("donor checkpoint is not bound to the committed preservation evidence")
    if str(proposal["evidence_fingerprint"]) != str(mat["phase13_evidence_fingerprint"]):
        raise RecoveryTargetReadinessError("donor checkpoint is not bound to the committed Phase-13 proposal")
    if not (
        str(mat["target_file_id"]) == str(stage["target_file_id"]) == str(proposal["target_file_id"])
        and str(mat["expected_revision_id"]) == str(stage["expected_revision_id"]) == str(proposal["expected_revision_id"])
        and str(mat["expected_sha256"]) == str(stage["expected_sha256"]) == str(proposal["expected_sha256"])
        and str(mat["recovery_intent_resolution_id"]) == str(stage["recovery_intent_resolution_id"])
        == str(proposal["recovery_intent_resolution_id"])
    ):
        raise RecoveryTargetReadinessError("recovery checkpoint identities do not form one coherent chain")

    file_row = conn.execute(
        "SELECT id,library_id,path,current_revision_id,health_status FROM files WHERE id=?",
        (str(mat["target_file_id"]),),
    ).fetchone()
    if file_row is None:
        raise RecoveryTargetReadinessError("target File is no longer registered")
    if int(file_row["library_id"]) != int(stage["library_id"]):
        raise RecoveryTargetReadinessError("target File changed Library ownership")
    if _canonical(file_row["path"]) != _canonical(stage["target_path"]):
        raise RecoveryTargetReadinessError("target File pathname changed since preservation review")
    if str(file_row["current_revision_id"]) != str(mat["expected_revision_id"]):
        raise RecoveryTargetReadinessError("target expected revision authority changed")
    if str(file_row["health_status"]) != "hash_mismatch":
        raise RecoveryTargetReadinessError("target integrity state is no longer hash_mismatch; run Verify / refresh recovery")

    latest = latest_mismatch_resolution(conn, str(mat["target_file_id"]))
    if latest is None or str(latest["resolution_id"]) != str(mat["recovery_intent_resolution_id"]):
        raise RecoveryTargetReadinessError("human recovery intent was superseded; refresh recovery planning")
    if str(latest["action"]) != ACTION_RETAIN_EXPECTED:
        raise RecoveryTargetReadinessError("latest human disposition no longer authorises recovery consideration")

    target_path = Path(str(stage["target_path"]))
    # Destination-directory topology is a prerequisite for even discussing a
    # later replacement.  Validate the exact source-tree parent before spending
    # time observing the target pathname; this also fails closed if a complete
    # Library tree has been renamed away and an attacker-controlled directory
    # now occupies the recorded source pathname.
    root_path, root_identity, parent_path, parent_identity = _verify_source_parent_authority(
        conn, library_id=int(stage["library_id"]), target_path=target_path
    )

    target, target_nlink = _observe_destination_target(
        target_path, expected_sha256=str(mat["expected_sha256"])
    )
    if target.state == "matches_expected":
        raise RecoveryTargetReadinessError("target already reproduces expected bytes; run Verify instead of replacement readiness")
    if not _target_observation_matches_stage(stage, target):
        raise RecoveryTargetReadinessError("target physical state changed since the preservation checkpoint")
    replacement_mode = MODE_RESTORE if target.state == "missing" else MODE_REPLACE

    # Re-prove Phase-14.0 operational evidence.
    preservation_path = None
    preservation_sha = None
    if str(stage["stage_state"]) == "suspect_bytes_preserved":
        preservation_path = str(stage["preservation_path"])
        preservation_sha = str(stage["preserved_sha256"])
        _attest_operational_file(
            conn,
            Path(preservation_path),
            expected_sha256=preservation_sha,
            expected_size=int(stage["preserved_size_bytes"]),
            label="preservation copy",
        )
    elif str(stage["stage_state"]) != "target_missing_no_preservation_required":
        raise RecoveryTargetReadinessError("unknown preservation stage state")

    _attest_operational_file(
        conn,
        Path(str(stage["manifest_path"])),
        expected_sha256=str(stage["manifest_sha256"]),
        expected_size=None,
        label="preservation manifest",
    )

    # Re-prove Phase-14.1 staged expected bytes and manifest representation.
    _attest_operational_file(
        conn,
        Path(str(mat["donor_materialization_path"])),
        expected_sha256=str(mat["donor_materialized_sha256"]),
        expected_size=int(mat["donor_materialized_size_bytes"]),
        label="materialized donor evidence",
    )
    if str(mat["donor_materialized_sha256"]) != str(mat["expected_sha256"]):
        raise RecoveryTargetReadinessError("materialized donor no longer represents the immutable expected revision")

    manifest_storage = str(mat["donor_manifest_storage"])
    if manifest_storage == MANIFEST_FILESYSTEM:
        _attest_operational_file(
            conn,
            Path(str(mat["donor_manifest_path"])),
            expected_sha256=str(mat["donor_manifest_sha256"]),
            expected_size=None,
            label="donor materialization manifest",
        )
    elif manifest_storage == MANIFEST_EMBEDDED:
        payload = mat["donor_manifest_payload_json"]
        if payload is None:
            raise RecoveryTargetReadinessError("embedded donor manifest payload is missing")
        raw_payload = str(payload).encode("utf-8")
        try:
            json.loads(str(payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryTargetReadinessError("embedded donor manifest is invalid JSON") from exc
        # The checkpoint SHA is over the exact canonical JSON representation
        # stored in the catalogue (pretty/sorted plus trailing newline), not a
        # newly re-serialized semantic equivalent.
        if hashlib.sha256(raw_payload).hexdigest() != str(mat["donor_manifest_sha256"]):
            raise RecoveryTargetReadinessError("embedded donor manifest no longer matches its immutable SHA-256")
    else:
        raise RecoveryTargetReadinessError("unknown donor manifest storage representation")

    # Phase 14.2.1/14.2.2/14.2.3: all potentially lengthy Phase-14 evidence hashing is
    # now complete. Re-establish and *bind* the exact registered Library root and
    # destination parent across the final target observation, then verify both
    # pathnames still reach those exact pinned objects before fingerprinting.
    (
        target, target_nlink, root_path, root_identity, parent_path, parent_identity
    ) = _assert_destination_snapshot_still_current(
        conn,
        library_id=int(stage["library_id"]),
        target_path=target_path,
        expected_sha256=str(mat["expected_sha256"]),
        initial_target=target,
        initial_target_nlink=target_nlink,
        initial_root_path=root_path,
        initial_root_identity=root_identity,
        initial_parent_path=parent_path,
        initial_parent_identity=parent_identity,
    )

    evidence = {
        "readiness_id": rid,
        "materialization_id": mid,
        "stage_id": str(mat["stage_id"]),
        "proposal_id": str(mat["proposal_id"]),
        "target": {
            "file_id": str(mat["target_file_id"]),
            "library_id": int(stage["library_id"]),
            "path": str(target_path),
            "state": target.state,
            "sha256": target.sha256,
            "size_bytes": target.size_bytes,
            "mtime_ns": target.mtime_ns,
            "fs_device_id": target.fs_device_id,
            "fs_object_id": target.fs_object_id,
            "link_count": target_nlink,
            "library_root_path": root_path,
            "library_root_fs_device_id": str(root_identity[0]),
            "library_root_fs_object_id": str(root_identity[1]),
            "parent_path": parent_path,
            "parent_fs_device_id": str(parent_identity[0]),
            "parent_fs_object_id": str(parent_identity[1]),
            "replacement_mode": replacement_mode,
        },
        "expected_revision_id": str(mat["expected_revision_id"]),
        "expected_sha256": str(mat["expected_sha256"]),
        "recovery_intent_resolution_id": str(mat["recovery_intent_resolution_id"]),
        "phase13_evidence_fingerprint": str(mat["phase13_evidence_fingerprint"]),
        "phase14_stage_fingerprint": str(mat["phase14_stage_fingerprint"]),
        "phase14_1_evidence_fingerprint": str(mat["evidence_fingerprint"]),
        "preservation_path": preservation_path,
        "preservation_sha256": preservation_sha,
        "preservation_manifest_sha256": str(stage["manifest_sha256"]),
        "donor_materialization_path": str(mat["donor_materialization_path"]),
        "donor_materialized_sha256": str(mat["donor_materialized_sha256"]),
        "donor_manifest_storage": manifest_storage,
        "donor_manifest_sha256": str(mat["donor_manifest_sha256"]),
        "readiness_state": READINESS_STATE,
        "target_replacement_authorized": False,
        "recovery_execution_authorized": False,
    }

    return RecoveryTargetReadiness(
        schema=READINESS_SCHEMA,
        readiness_id=rid,
        readiness_state=READINESS_STATE,
        materialization_id=mid,
        stage_id=str(mat["stage_id"]),
        proposal_id=str(mat["proposal_id"]),
        file_id=str(mat["target_file_id"]),
        library_id=int(stage["library_id"]),
        expected_revision_id=str(mat["expected_revision_id"]),
        expected_sha256=str(mat["expected_sha256"]),
        recovery_intent_resolution_id=str(mat["recovery_intent_resolution_id"]),
        target_path=str(target_path),
        target_state=target.state,
        target_observed_sha256=target.sha256,
        target_size_bytes=target.size_bytes,
        target_mtime_ns=target.mtime_ns,
        target_fs_device_id=target.fs_device_id,
        target_fs_object_id=target.fs_object_id,
        target_link_count=target_nlink,
        library_root_path=root_path,
        library_root_fs_device_id=str(root_identity[0]),
        library_root_fs_object_id=str(root_identity[1]),
        target_parent_path=parent_path,
        target_parent_fs_device_id=str(parent_identity[0]),
        target_parent_fs_object_id=str(parent_identity[1]),
        replacement_mode=replacement_mode,
        preservation_path=preservation_path,
        preservation_sha256=preservation_sha,
        preservation_manifest_path=str(stage["manifest_path"]),
        preservation_manifest_sha256=str(stage["manifest_sha256"]),
        donor_materialization_path=str(mat["donor_materialization_path"]),
        donor_materialized_sha256=str(mat["donor_materialized_sha256"]),
        donor_materialized_size_bytes=int(mat["donor_materialized_size_bytes"]),
        donor_manifest_storage=manifest_storage,
        donor_manifest_path=str(mat["donor_manifest_path"]),
        donor_manifest_sha256=str(mat["donor_manifest_sha256"]),
        target_replacement_authorized=False,
        recovery_execution_authorized=False,
        evidence_fingerprint=_fingerprint(evidence),
    )


def record_target_replacement_readiness(
    conn: Connection,
    readiness: RecoveryTargetReadiness,
    *,
    note: str | None = None,
) -> RecordedTargetReadiness:
    """Append one immutable readiness checkpoint after revalidation.

    Only SQLite audit state is written.  No source or operational filesystem
    object is modified.  A later replacement phase must re-attest everything
    again and cannot treat this row as executable authority.
    """
    if readiness.schema != READINESS_SCHEMA:
        raise RecoveryTargetReadinessError("invalid target-readiness schema")
    if readiness.target_replacement_authorized or readiness.recovery_execution_authorized:
        raise RecoveryTargetReadinessError("Phase 14.2 readiness contains forbidden execution authority")
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise RecoveryTargetReadinessError("target-readiness note is too long")

    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM archive_recovery_target_readiness WHERE materialization_id=? LIMIT 1",
            (readiness.materialization_id,),
        ).fetchone() is not None:
            raise RecoveryTargetReadinessError(
                "this donor materialization already has a recorded target-readiness checkpoint"
            )
        rebuilt = build_target_replacement_readiness(
            conn,
            materialization_id=readiness.materialization_id,
            readiness_id=readiness.readiness_id,
        )
        if rebuilt.evidence_fingerprint != readiness.evidence_fingerprint:
            raise RecoveryTargetReadinessError("target-replacement readiness changed; rebuild before recording")
        assessed_at = _now()
        conn.execute(
            """
            INSERT INTO archive_recovery_target_readiness(
                readiness_id,materialization_id,stage_id,proposal_id,target_file_id,library_id,
                expected_revision_id,expected_sha256,recovery_intent_resolution_id,
                target_path,target_state,target_observed_sha256,target_size_bytes,target_mtime_ns,
                target_fs_device_id,target_fs_object_id,target_link_count,
                target_parent_path,target_parent_fs_device_id,target_parent_fs_object_id,
                replacement_mode,preservation_path,preservation_sha256,preservation_manifest_path,
                preservation_manifest_sha256,donor_materialization_path,donor_materialized_sha256,
                donor_materialized_size_bytes,donor_manifest_storage,donor_manifest_path,
                donor_manifest_sha256,readiness_state,target_replacement_authorized,
                recovery_execution_authorized,evidence_fingerprint,note,assessed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rebuilt.readiness_id,rebuilt.materialization_id,rebuilt.stage_id,rebuilt.proposal_id,
                rebuilt.file_id,rebuilt.library_id,rebuilt.expected_revision_id,rebuilt.expected_sha256,
                rebuilt.recovery_intent_resolution_id,rebuilt.target_path,rebuilt.target_state,
                rebuilt.target_observed_sha256,rebuilt.target_size_bytes,rebuilt.target_mtime_ns,
                rebuilt.target_fs_device_id,rebuilt.target_fs_object_id,rebuilt.target_link_count,
                rebuilt.target_parent_path,rebuilt.target_parent_fs_device_id,rebuilt.target_parent_fs_object_id,
                rebuilt.replacement_mode,rebuilt.preservation_path,rebuilt.preservation_sha256,
                rebuilt.preservation_manifest_path,rebuilt.preservation_manifest_sha256,
                rebuilt.donor_materialization_path,rebuilt.donor_materialized_sha256,
                rebuilt.donor_materialized_size_bytes,rebuilt.donor_manifest_storage,
                rebuilt.donor_manifest_path,rebuilt.donor_manifest_sha256,rebuilt.readiness_state,
                0,0,rebuilt.evidence_fingerprint,note,assessed_at,
            ),
        )
        conn.execute(
            "INSERT INTO integrity_events(file_id,event_type,detail) VALUES (?,?,?)",
            (
                rebuilt.file_id,
                "archive_recovery_target_readiness_recorded",
                f"Phase-14.2 target-replacement readiness {rebuilt.readiness_id} recorded from donor "
                f"materialization {rebuilt.materialization_id}; replacement_mode={rebuilt.replacement_mode}. "
                "Target replacement and recovery execution remain NOT authorised.",
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return RecordedTargetReadiness(
        readiness_id=rebuilt.readiness_id,
        materialization_id=rebuilt.materialization_id,
        file_id=rebuilt.file_id,
        evidence_fingerprint=rebuilt.evidence_fingerprint,
        assessed_at=assessed_at,
    )


def concise_readiness_text(readiness: RecoveryTargetReadiness) -> str:
    return "\n".join([
        "Phase 14.2 — Target-Replacement Readiness Protocol",
        f"Readiness ID: {readiness.readiness_id}",
        f"Target: {readiness.file_id}  {readiness.target_path}",
        f"Target state: {readiness.target_state}",
        f"Replacement mode: {readiness.replacement_mode}",
        f"Expected SHA-256: {readiness.expected_sha256}",
        f"Evidence state: {readiness.readiness_state}",
        "Target replacement authorised: NO",
        "Recovery execution authorised: NO",
        "Next boundary: separately reviewed replacement execution protocol",
    ])
