"""Phase 14.3 — explicit target-replacement execution.

This is the first PPA recovery phase that may intentionally mutate a registered
source-photo namespace.  It therefore consumes only one immutable, freshly
revalidated Phase-14.2 readiness checkpoint and requires an exact plan-derived
human confirmation before a durable one-attempt execution intent is committed.

The execution module deliberately does *not* certify catalogue health.  Even
when expected bytes are placed and descriptor-verified, the File remains in its
existing ``hash_mismatch`` state until the ordinary Verify path independently
reconciles it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
from sqlite3 import Connection
import stat
import uuid

from ppa.recovery_target_readiness import (
    MODE_REPLACE,
    MODE_RESTORE,
    READINESS_STATE,
    RecoveryTargetReadiness,
    RecoveryTargetReadinessError,
    build_target_replacement_readiness,
)
from ppa.secure_write import (
    BoundTemporaryFile,
    SecureWriteError,
    SecureWriteTransitionError,
    WindowsDirectoryPin,
    bind_directory_authority,
    is_windows_reparse_point_stat,
    windows_path_has_reparse_component,
)

EXECUTION_PLAN_SCHEMA = "ppa-recovery-target-replacement-execution-plan/1"
EXECUTION_RESULT_SCHEMA = "ppa-recovery-target-replacement-execution-result/1"
AUTHORIZATION_STATE = "confirmed_one_attempt"
RESULT_PLACED = "expected_target_placed_verified"
RESULT_ABORTED = "aborted_before_target_transition"
RESULT_RESTORED = "aborted_exact_target_restored"


class RecoveryTargetExecutionError(ValueError):
    """The reviewed target-replacement execution cannot safely proceed."""


@dataclass(frozen=True)
class RecoveryTargetExecutionPlan:
    schema: str
    execution_id: str
    readiness_id: str
    materialization_id: str
    file_id: str
    library_id: int
    expected_revision_id: str
    expected_sha256: str
    recovery_intent_resolution_id: str
    replacement_mode: str
    target_path: str
    target_initial_state: str
    target_initial_sha256: str | None
    target_initial_size_bytes: int | None
    target_initial_mtime_ns: int | None
    target_initial_fs_device_id: str | None
    target_initial_fs_object_id: str | None
    target_initial_link_count: int | None
    library_root_path: str
    library_root_fs_device_id: str
    library_root_fs_object_id: str
    target_parent_path: str
    target_parent_fs_device_id: str
    target_parent_fs_object_id: str
    donor_materialization_path: str
    donor_materialized_sha256: str
    donor_materialized_size_bytes: int
    readiness_evidence_fingerprint: str
    target_replacement_authorized: bool
    recovery_execution_authorized: bool
    execution_plan_fingerprint: str
    confirmation_phrase: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )


@dataclass(frozen=True)
class RecordedRecoveryExecutionAttempt:
    execution_id: str
    readiness_id: str
    file_id: str
    authorization_state: str
    execution_plan_fingerprint: str
    authorized_at: str


@dataclass(frozen=True)
class RecoveryTargetExecutionResult:
    schema: str
    result_id: str
    execution_id: str
    readiness_id: str
    file_id: str
    result_state: str
    installed_sha256: str | None
    installed_size_bytes: int | None
    installed_fs_device_id: str | None
    installed_fs_object_id: str | None
    installed_link_count: int | None
    suspect_retained_path: str | None
    suspect_sha256: str | None
    suspect_size_bytes: int | None
    suspect_fs_device_id: str | None
    suspect_fs_object_id: str | None
    source_namespace_changed: bool
    verify_reconciliation_required: bool
    evidence_fingerprint: str
    detail: str | None
    completed_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )


@dataclass(frozen=True)
class RecoveryExecutionStatus:
    execution_id: str
    readiness_id: str
    file_id: str
    resolved: bool
    result_state: str | None
    target_state: str
    target_sha256: str | None
    suspect_retained_path: str | None
    detail: str


@dataclass(frozen=True)
class _FileSnapshot:
    sha256: str
    size_bytes: int
    mtime_ns: int
    fs_device_id: str
    fs_object_id: str
    link_count: int


@dataclass(frozen=True)
class _PhysicalOutcome:
    result_state: str
    installed: _FileSnapshot | None
    suspect_path: str | None
    suspect: _FileSnapshot | None
    source_namespace_changed: bool
    detail: str


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validated_uuid(value: str | None, *, label: str) -> str:
    if value is None:
        return str(uuid.uuid4())
    raw = str(value).strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise RecoveryTargetExecutionError(f"{label} must be a canonical UUID") from exc
    if raw != str(parsed):
        raise RecoveryTargetExecutionError(f"{label} must be a canonical UUID")
    return raw


def _confirmation_phrase(execution_id: str, execution_plan_fingerprint: str) -> str:
    return f"EXECUTE PPA RECOVERY {execution_id} {execution_plan_fingerprint[:16]}"


def _attempt_exists(conn: Connection, *, execution_id: str | None = None, readiness_id: str | None = None) -> bool:
    if execution_id is not None:
        return conn.execute(
            "SELECT 1 FROM archive_recovery_target_execution_attempts WHERE execution_id=? LIMIT 1",
            (execution_id,),
        ).fetchone() is not None
    assert readiness_id is not None
    return conn.execute(
        "SELECT 1 FROM archive_recovery_target_execution_attempts WHERE readiness_id=? LIMIT 1",
        (readiness_id,),
    ).fetchone() is not None


def _unresolved_attempt_for_file(conn: Connection, file_id: str, *, except_execution_id: str | None = None):
    sql = (
        "SELECT a.* FROM archive_recovery_target_execution_attempts a "
        "LEFT JOIN archive_recovery_target_execution_results r ON r.execution_id=a.execution_id "
        "WHERE a.target_file_id=? AND r.execution_id IS NULL"
    )
    params: list[object] = [file_id]
    if except_execution_id is not None:
        sql += " AND a.execution_id<>?"
        params.append(except_execution_id)
    sql += " ORDER BY a.id DESC LIMIT 1"
    return conn.execute(sql, tuple(params)).fetchone()


def _windows_local_ntfs(path: Path) -> bool:
    if os.name != "nt":
        return False
    raw = os.path.abspath(os.fspath(path))
    if raw.startswith("\\\\"):
        return False
    drive, _ = os.path.splitdrive(raw)
    if not drive:
        return False
    root = drive + "\\"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint
    # DRIVE_FIXED == 3.  Phase 14.3 intentionally starts with local fixed disks.
    if int(get_drive_type(root)) != 3:
        return False

    fs_name = ctypes.create_unicode_buffer(64)
    get_volume = kernel32.GetVolumeInformationW
    get_volume.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint), ctypes.c_wchar_p, ctypes.c_uint,
    ]
    get_volume.restype = ctypes.c_int
    serial = ctypes.c_uint()
    max_component = ctypes.c_uint()
    flags = ctypes.c_uint()
    ok = get_volume(
        root, None, 0, ctypes.byref(serial), ctypes.byref(max_component), ctypes.byref(flags),
        fs_name, len(fs_name),
    )
    return bool(ok) and fs_name.value.upper() == "NTFS"


def _posix_known_network_mount(path: Path) -> bool:
    """Best-effort rejection of known network mount types on Linux.

    Unsupported/unknown filesystems still have to satisfy the hardened bound
    directory + atomic no-replace primitives at execution time.  This helper is
    an additional refusal for mount types whose semantics are explicitly remote.
    """
    if os.name == "nt" or not Path("/proc/mounts").exists():
        return False
    network_types = {
        "nfs", "nfs4", "cifs", "smbfs", "sshfs", "fuse.sshfs", "9p",
        "afs", "ceph", "glusterfs", "davfs", "fuse.rclone",
    }
    canonical = os.path.realpath(os.fspath(path))
    best_len = -1
    best_type = None
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mount = fields[1].replace("\\040", " ")
                try:
                    inside = os.path.commonpath([canonical, mount]) == os.path.normpath(mount)
                except ValueError:
                    inside = False
                if inside and len(mount) > best_len:
                    best_len = len(mount)
                    best_type = fields[2]
    except OSError:
        return False
    return best_type in network_types


def _assert_platform_supported(readiness: RecoveryTargetReadiness) -> None:
    target = Path(readiness.target_path)
    if os.name == "nt":
        if not _windows_local_ntfs(target):
            raise RecoveryTargetExecutionError(
                "Phase 14.3 execution requires a local fixed NTFS Library on Windows"
            )
        return
    if _posix_known_network_mount(target):
        raise RecoveryTargetExecutionError("Phase 14.3 refuses known remote/network filesystem semantics")
    if readiness.replacement_mode == MODE_REPLACE:
        raise RecoveryTargetExecutionError(
            "Phase 14.3 existing-target replacement is native-Windows-only; "
            "POSIX exact existing-object rename authority is intentionally not claimed"
        )


def _load_recorded_readiness(conn: Connection, readiness_id: str):
    row = conn.execute(
        "SELECT * FROM archive_recovery_target_readiness WHERE readiness_id=?",
        (readiness_id,),
    ).fetchone()
    if row is None:
        raise RecoveryTargetExecutionError("unknown recorded Phase-14.2 target-readiness checkpoint")
    if row["readiness_state"] != READINESS_STATE:
        raise RecoveryTargetExecutionError("recorded target-readiness state is not executable review evidence")
    if int(row["target_replacement_authorized"] or 0) != 0 or int(row["recovery_execution_authorized"] or 0) != 0:
        raise RecoveryTargetExecutionError("recorded Phase-14.2 readiness unexpectedly contains execution authority")
    return row


def _build_plan(
    conn: Connection,
    *,
    readiness_id: str,
    execution_id: str | None,
    allow_existing_execution_id: str | None = None,
) -> RecoveryTargetExecutionPlan:
    rid = _validated_uuid(readiness_id, label="readiness ID")
    eid = _validated_uuid(execution_id, label="execution ID")
    recorded = _load_recorded_readiness(conn, rid)

    if allow_existing_execution_id is None:
        if _attempt_exists(conn, execution_id=eid):
            raise RecoveryTargetExecutionError("execution ID has already been consumed")
        if _attempt_exists(conn, readiness_id=rid):
            raise RecoveryTargetExecutionError("this readiness checkpoint already has an execution attempt")
    elif eid != allow_existing_execution_id:
        raise RecoveryTargetExecutionError("authorized execution identity changed")

    unresolved = _unresolved_attempt_for_file(
        conn, str(recorded["target_file_id"]), except_execution_id=allow_existing_execution_id
    )
    if unresolved is not None:
        raise RecoveryTargetExecutionError(
            f"target File already has unresolved recovery execution {unresolved['execution_id']}; reconcile/review first"
        )

    try:
        readiness = build_target_replacement_readiness(
            conn,
            materialization_id=str(recorded["materialization_id"]),
            readiness_id=rid,
        )
    except RecoveryTargetReadinessError as exc:
        raise RecoveryTargetExecutionError(f"Phase-14.2 readiness revalidation failed: {exc}") from exc
    if readiness.evidence_fingerprint != str(recorded["evidence_fingerprint"]):
        raise RecoveryTargetExecutionError("recorded Phase-14.2 readiness is stale; record a fresh readiness checkpoint")
    if readiness.replacement_mode != str(recorded["replacement_mode"]):
        raise RecoveryTargetExecutionError("recorded replacement mode changed during revalidation")

    _assert_platform_supported(readiness)

    evidence = {
        "execution_id": eid,
        "readiness_id": rid,
        "materialization_id": readiness.materialization_id,
        "target": {
            "file_id": readiness.file_id,
            "library_id": readiness.library_id,
            "expected_revision_id": readiness.expected_revision_id,
            "expected_sha256": readiness.expected_sha256,
            "recovery_intent_resolution_id": readiness.recovery_intent_resolution_id,
            "replacement_mode": readiness.replacement_mode,
            "path": readiness.target_path,
            "state": readiness.target_state,
            "observed_sha256": readiness.target_observed_sha256,
            "size_bytes": readiness.target_size_bytes,
            "mtime_ns": readiness.target_mtime_ns,
            "fs_device_id": readiness.target_fs_device_id,
            "fs_object_id": readiness.target_fs_object_id,
            "link_count": readiness.target_link_count,
        },
        "root": {
            "path": readiness.library_root_path,
            "fs_device_id": readiness.library_root_fs_device_id,
            "fs_object_id": readiness.library_root_fs_object_id,
        },
        "parent": {
            "path": readiness.target_parent_path,
            "fs_device_id": readiness.target_parent_fs_device_id,
            "fs_object_id": readiness.target_parent_fs_object_id,
        },
        "donor": {
            "path": readiness.donor_materialization_path,
            "sha256": readiness.donor_materialized_sha256,
            "size_bytes": readiness.donor_materialized_size_bytes,
        },
        "readiness_evidence_fingerprint": readiness.evidence_fingerprint,
        "target_replacement_authorized": False,
        "recovery_execution_authorized": False,
    }
    fp = _fingerprint(evidence)
    return RecoveryTargetExecutionPlan(
        schema=EXECUTION_PLAN_SCHEMA,
        execution_id=eid,
        readiness_id=rid,
        materialization_id=readiness.materialization_id,
        file_id=readiness.file_id,
        library_id=readiness.library_id,
        expected_revision_id=readiness.expected_revision_id,
        expected_sha256=readiness.expected_sha256,
        recovery_intent_resolution_id=readiness.recovery_intent_resolution_id,
        replacement_mode=readiness.replacement_mode,
        target_path=readiness.target_path,
        target_initial_state=readiness.target_state,
        target_initial_sha256=readiness.target_observed_sha256,
        target_initial_size_bytes=readiness.target_size_bytes,
        target_initial_mtime_ns=readiness.target_mtime_ns,
        target_initial_fs_device_id=readiness.target_fs_device_id,
        target_initial_fs_object_id=readiness.target_fs_object_id,
        target_initial_link_count=readiness.target_link_count,
        library_root_path=readiness.library_root_path,
        library_root_fs_device_id=readiness.library_root_fs_device_id,
        library_root_fs_object_id=readiness.library_root_fs_object_id,
        target_parent_path=readiness.target_parent_path,
        target_parent_fs_device_id=readiness.target_parent_fs_device_id,
        target_parent_fs_object_id=readiness.target_parent_fs_object_id,
        donor_materialization_path=readiness.donor_materialization_path,
        donor_materialized_sha256=readiness.donor_materialized_sha256,
        donor_materialized_size_bytes=readiness.donor_materialized_size_bytes,
        readiness_evidence_fingerprint=readiness.evidence_fingerprint,
        target_replacement_authorized=False,
        recovery_execution_authorized=False,
        execution_plan_fingerprint=fp,
        confirmation_phrase=_confirmation_phrase(eid, fp),
    )


def build_target_replacement_execution_plan(
    conn: Connection,
    *,
    readiness_id: str,
    execution_id: str | None = None,
) -> RecoveryTargetExecutionPlan:
    """Build a fresh non-authoritative Phase-14.3 execution preview."""
    return _build_plan(
        conn, readiness_id=readiness_id, execution_id=execution_id,
        allow_existing_execution_id=None,
    )


def _validate_plan(plan: RecoveryTargetExecutionPlan) -> None:
    if plan.schema != EXECUTION_PLAN_SCHEMA:
        raise RecoveryTargetExecutionError("invalid recovery execution plan schema")
    if plan.target_replacement_authorized or plan.recovery_execution_authorized:
        raise RecoveryTargetExecutionError("preview plan contains forbidden pre-confirmation execution authority")
    if _confirmation_phrase(plan.execution_id, plan.execution_plan_fingerprint) != plan.confirmation_phrase:
        raise RecoveryTargetExecutionError("execution confirmation phrase is not bound to this plan")


def _authorize_attempt(
    conn: Connection,
    plan: RecoveryTargetExecutionPlan,
    *,
    confirmation: str,
    note: str | None,
) -> tuple[RecoveryTargetExecutionPlan, RecordedRecoveryExecutionAttempt]:
    _validate_plan(plan)
    if str(confirmation) != plan.confirmation_phrase:
        raise RecoveryTargetExecutionError("exact Phase-14.3 confirmation phrase was not supplied")
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise RecoveryTargetExecutionError("execution note is too long")

    try:
        conn.execute("BEGIN IMMEDIATE")
        if _attempt_exists(conn, execution_id=plan.execution_id):
            raise RecoveryTargetExecutionError("execution ID has already been consumed")
        if _attempt_exists(conn, readiness_id=plan.readiness_id):
            raise RecoveryTargetExecutionError("this readiness checkpoint already has an execution attempt")
        rebuilt = _build_plan(
            conn,
            readiness_id=plan.readiness_id,
            execution_id=plan.execution_id,
            allow_existing_execution_id=None,
        )
        if rebuilt.execution_plan_fingerprint != plan.execution_plan_fingerprint:
            raise RecoveryTargetExecutionError("execution plan changed; preview and confirm again")
        authorized_at = _now()
        conn.execute(
            """
            INSERT INTO archive_recovery_target_execution_attempts(
                execution_id,readiness_id,materialization_id,target_file_id,library_id,
                expected_revision_id,expected_sha256,recovery_intent_resolution_id,replacement_mode,
                target_path,target_initial_state,target_initial_sha256,target_initial_size_bytes,
                target_initial_mtime_ns,target_initial_fs_device_id,target_initial_fs_object_id,
                target_initial_link_count,library_root_path,library_root_fs_device_id,
                library_root_fs_object_id,target_parent_path,target_parent_fs_device_id,
                target_parent_fs_object_id,donor_materialization_path,donor_materialized_sha256,
                donor_materialized_size_bytes,readiness_evidence_fingerprint,execution_plan_fingerprint,
                confirmation_phrase_sha256,authorization_state,target_replacement_authorized,
                recovery_execution_authorized,note,authorized_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rebuilt.execution_id,rebuilt.readiness_id,rebuilt.materialization_id,rebuilt.file_id,
                rebuilt.library_id,rebuilt.expected_revision_id,rebuilt.expected_sha256,
                rebuilt.recovery_intent_resolution_id,rebuilt.replacement_mode,rebuilt.target_path,
                rebuilt.target_initial_state,rebuilt.target_initial_sha256,rebuilt.target_initial_size_bytes,
                rebuilt.target_initial_mtime_ns,rebuilt.target_initial_fs_device_id,
                rebuilt.target_initial_fs_object_id,rebuilt.target_initial_link_count,
                rebuilt.library_root_path,rebuilt.library_root_fs_device_id,
                rebuilt.library_root_fs_object_id,rebuilt.target_parent_path,
                rebuilt.target_parent_fs_device_id,rebuilt.target_parent_fs_object_id,
                rebuilt.donor_materialization_path,rebuilt.donor_materialized_sha256,
                rebuilt.donor_materialized_size_bytes,rebuilt.readiness_evidence_fingerprint,
                rebuilt.execution_plan_fingerprint,
                hashlib.sha256(rebuilt.confirmation_phrase.encode("utf-8")).hexdigest(),
                AUTHORIZATION_STATE,1,1,note,authorized_at,
            ),
        )
        conn.execute(
            "INSERT INTO integrity_events(file_id,event_type,detail) VALUES (?,?,?)",
            (
                rebuilt.file_id,
                "archive_recovery_target_execution_authorized",
                f"Phase-14.3 one-attempt recovery execution {rebuilt.execution_id} authorised from "
                f"readiness {rebuilt.readiness_id}; replacement_mode={rebuilt.replacement_mode}.",
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return rebuilt, RecordedRecoveryExecutionAttempt(
        execution_id=rebuilt.execution_id,
        readiness_id=rebuilt.readiness_id,
        file_id=rebuilt.file_id,
        authorization_state=AUTHORIZATION_STATE,
        execution_plan_fingerprint=rebuilt.execution_plan_fingerprint,
        authorized_at=authorized_at,
    )


def _open_regular_path_fd(path: Path) -> int:
    if windows_path_has_reparse_component(path):
        raise RecoveryTargetExecutionError(f"unsafe reparse traversal while opening {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise RecoveryTargetExecutionError(f"could not open exact recovery evidence: {path}") from exc


def _hash_fd_snapshot(
    fd: int,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    expected_identity: tuple[int, int] | None = None,
    expected_mtime_ns: int | None = None,
    label: str,
) -> _FileSnapshot:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or is_windows_reparse_point_stat(before):
        raise RecoveryTargetExecutionError(f"{label} is not a safe regular file")
    identity = (int(before.st_dev), int(before.st_ino))
    if expected_identity is not None and identity != tuple(expected_identity):
        raise RecoveryTargetExecutionError(f"{label} filesystem identity changed")
    if int(getattr(before, "st_nlink", 1) or 1) != 1:
        raise RecoveryTargetExecutionError(f"{label} has hard-link aliases")
    if expected_mtime_ns is not None and int(before.st_mtime_ns) != int(expected_mtime_ns):
        raise RecoveryTargetExecutionError(f"{label} mtime changed")

    digest = hashlib.sha256()
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    after = os.fstat(fd)
    after_identity = (int(after.st_dev), int(after.st_ino))
    if after_identity != identity:
        raise RecoveryTargetExecutionError(f"{label} identity changed during descriptor-bound hash")
    if int(getattr(after, "st_nlink", 1) or 1) != 1:
        raise RecoveryTargetExecutionError(f"{label} gained a hard-link alias during descriptor-bound hash")
    if int(after.st_size) != int(before.st_size) or int(after.st_mtime_ns) != int(before.st_mtime_ns):
        raise RecoveryTargetExecutionError(f"{label} changed during descriptor-bound hash")
    sha = digest.hexdigest()
    if expected_sha256 is not None and sha != str(expected_sha256):
        raise RecoveryTargetExecutionError(f"{label} SHA-256 changed")
    if expected_size is not None and total != int(expected_size):
        raise RecoveryTargetExecutionError(f"{label} size changed")
    return _FileSnapshot(
        sha256=sha,
        size_bytes=total,
        mtime_ns=int(after.st_mtime_ns),
        fs_device_id=str(identity[0]),
        fs_object_id=str(identity[1]),
        link_count=1,
    )


def _copy_donor_to_temp(plan: RecoveryTargetExecutionPlan, temp: BoundTemporaryFile) -> None:
    source = Path(plan.donor_materialization_path)
    fd = _open_regular_path_fd(source)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or int(getattr(before, "st_nlink", 1) or 1) != 1:
            raise RecoveryTargetExecutionError("materialized donor evidence is not a single-link regular file")
        digest = hashlib.sha256()
        total = 0
        with temp.binary_writer() as out:
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                out.write(chunk)
        after = os.fstat(fd)
        if (
            (int(before.st_dev), int(before.st_ino)) != (int(after.st_dev), int(after.st_ino))
            or int(before.st_size) != int(after.st_size)
            or int(before.st_mtime_ns) != int(after.st_mtime_ns)
            or int(getattr(after, "st_nlink", 1) or 1) != 1
        ):
            raise RecoveryTargetExecutionError("materialized donor evidence changed during execution copy")
        if digest.hexdigest() != plan.expected_sha256 or total != int(plan.donor_materialized_size_bytes):
            raise RecoveryTargetExecutionError("materialized donor no longer reproduces the immutable expected revision")
        temp_sha, temp_size = temp.hash_and_size()
        if temp_sha != plan.expected_sha256 or temp_size != total:
            raise RecoveryTargetExecutionError("secured recovery temporary does not reproduce expected bytes")
    finally:
        os.close(fd)


def _verify_installed_path(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_identity: tuple[int, int],
) -> _FileSnapshot:
    fd = _open_regular_path_fd(path)
    try:
        return _hash_fd_snapshot(
            fd,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            expected_identity=expected_identity,
            label="installed recovery target",
        )
    finally:
        os.close(fd)


def _bind_final_topology(plan: RecoveryTargetExecutionPlan):
    root = None
    parent = None
    try:
        root = bind_directory_authority(Path(plan.library_root_path))
        if tuple(root.identity) != (
            int(plan.library_root_fs_device_id), int(plan.library_root_fs_object_id)
        ):
            raise RecoveryTargetExecutionError("registered Library root identity changed before execution")
        parent = bind_directory_authority(Path(plan.target_parent_path))
        if tuple(parent.identity) != (
            int(plan.target_parent_fs_device_id), int(plan.target_parent_fs_object_id)
        ):
            raise RecoveryTargetExecutionError("target-parent identity changed before execution")
        return root, parent
    except BaseException:
        if parent is not None:
            parent.close()
        if root is not None:
            root.close()
        raise


def _execute_missing_restore(plan: RecoveryTargetExecutionPlan) -> _PhysicalOutcome:
    target = Path(plan.target_path)
    root, parent = _bind_final_topology(plan)
    installed = False
    try:
        temp_identity: tuple[int, int] | None = None
        try:
            with BoundTemporaryFile.create(
                target.parent,
                prefix=f".{target.name}.ppa-recovery-{plan.execution_id[:8]}-",
                suffix=".tmp",
                expected_parent_identity=(
                    int(plan.target_parent_fs_device_id), int(plan.target_parent_fs_object_id)
                ),
            ) as temp:
                _copy_donor_to_temp(plan, temp)
                temp_identity = tuple(temp.identity)
                temp.install(target, replace=False)
                installed = True
            assert temp_identity is not None
            snap = _verify_installed_path(
                target,
                expected_sha256=plan.expected_sha256,
                expected_size=plan.donor_materialized_size_bytes,
                expected_identity=temp_identity,
            )
            root.verify_pathname()
            parent.verify_pathname()
            return _PhysicalOutcome(
                result_state=RESULT_PLACED,
                installed=snap,
                suspect_path=None,
                suspect=None,
                source_namespace_changed=True,
                detail="missing target restored with descriptor-verified expected bytes",
            )
        except SecureWriteTransitionError as exc:
            # The secure-write layer positively reports that the target name was
            # acquired before a later durability/verification failure.  A result
            # row would falsely resolve the durable attempt, so leave it unresolved.
            raise RecoveryTargetExecutionError(
                "recovery target namespace transition occurred but final attestation did not complete; "
                "attempt remains unresolved"
            ) from exc
        except (RecoveryTargetExecutionError, SecureWriteError, OSError) as exc:
            if not installed:
                return _PhysicalOutcome(
                    result_state=RESULT_ABORTED,
                    installed=None,
                    suspect_path=None,
                    suspect=None,
                    source_namespace_changed=False,
                    detail=f"restore aborted before target transition: {exc}",
                )
            raise RecoveryTargetExecutionError(
                "recovery target was installed but final attestation did not complete; attempt remains unresolved"
            ) from exc
    finally:
        parent.close()
        root.close()


def _open_windows_target_fd(parent: WindowsDirectoryPin, name: str) -> int:
    if os.name != "nt":
        raise RecoveryTargetExecutionError("native Windows target handle is unavailable")
    desired = (
        parent._FILE_READ_DATA | parent._FILE_READ_ATTRIBUTES | parent._DELETE | parent._SYNCHRONIZE
    )
    options = (
        parent._FILE_NON_DIRECTORY_FILE | parent._FILE_SYNCHRONOUS_IO_NONALERT | parent._FILE_OPEN_REPARSE_POINT
    )
    handle = parent._nt_open_relative(
        name,
        desired_access=desired,
        disposition=parent._FILE_OPEN,
        create_options=options,
    )
    try:
        if parent._native_is_unsafe_entry(handle, allow_directory=False):
            raise RecoveryTargetExecutionError("target handle is not a safe regular file")
        import msvcrt
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        handle = 0  # ownership transferred to CRT descriptor
        return fd
    finally:
        if handle:
            parent._close_native_handle(handle)


def _restore_parked_windows_target(
    parent: WindowsDirectoryPin,
    *,
    fd: int,
    parked_name: str,
    target_name: str,
    original: _FileSnapshot,
) -> bool:
    """Restore only when the reverse transition is proven before *and* after.

    Before the reverse rename, both the original still-open suspect handle and a
    fresh handle opened through the parked pathname must prove the exact reviewed
    bytes/object.  After the exact-handle rename back to ``target_name``, the same
    original handle and a newly opened target-path handle must prove those bytes
    and identity again.

    Pre-transition uncertainty returns ``False``.  Once the reverse rename has
    succeeded, any failed final proof raises ``RecoveryTargetExecutionError`` so
    the durable attempt remains unresolved; a post-transition failure must never
    be collapsed into "not restored" or an immutable exact-restore result.
    """
    # Never replace a late-arriving target.
    if parent.child_info_or_none(target_name) is not None:
        return False

    expected_identity = (int(original.fs_device_id), int(original.fs_object_id))
    fresh_parked_fd = -1
    try:
        # PRE-RESTORE: prove the original exact handle still describes the
        # reviewed suspect object and bytes.
        handle_snapshot = _hash_fd_snapshot(
            fd,
            expected_sha256=original.sha256,
            expected_size=original.size_bytes,
            expected_identity=expected_identity,
            expected_mtime_ns=original.mtime_ns,
            label="parked suspect target handle",
        )

        # Independently prove the parked pathname, relative to the pinned parent,
        # names that same exact unchanged object.
        fresh_parked_fd = _open_windows_target_fd(parent, parked_name)
        path_snapshot = _hash_fd_snapshot(
            fresh_parked_fd,
            expected_sha256=original.sha256,
            expected_size=original.size_bytes,
            expected_identity=expected_identity,
            expected_mtime_ns=original.mtime_ns,
            label="parked suspect target pathname",
        )
        if handle_snapshot != path_snapshot:
            return False

        # Re-establish destination absence after both proofs.
        if parent.child_info_or_none(target_name) is not None:
            return False
    except Exception:
        return False
    finally:
        if fresh_parked_fd >= 0:
            try:
                os.close(fresh_parked_fd)
            except OSError:
                pass

    # REVERSE TRANSITION.  From this point onward, failure cannot truthfully be
    # described as "not restored": the namespace may already name this object at
    # the registered target.
    try:
        parent.rename_fd(fd, target_name, replace=False)
    except Exception:
        return False

    fresh_target_fd = -1
    try:
        # POST-RESTORE: re-attest the same exact original handle *after* the
        # reverse rename.  This catches concurrent in-place writes arriving in
        # the pre-proof -> rename window while filesystem identity stays stable.
        restored_handle = _hash_fd_snapshot(
            fd,
            expected_sha256=original.sha256,
            expected_size=original.size_bytes,
            expected_identity=expected_identity,
            expected_mtime_ns=original.mtime_ns,
            label="restored suspect target handle",
        )

        # Independently reopen the restored target through the pinned parent and
        # prove that the pathname names the same exact reviewed bytes/object.
        fresh_target_fd = _open_windows_target_fd(parent, target_name)
        restored_path = _hash_fd_snapshot(
            fresh_target_fd,
            expected_sha256=original.sha256,
            expected_size=original.size_bytes,
            expected_identity=expected_identity,
            expected_mtime_ns=original.mtime_ns,
            label="restored suspect target pathname",
        )
        if restored_handle != restored_path:
            raise RecoveryTargetExecutionError(
                "restored suspect handle/path proofs disagree after reverse transition"
            )
        if parent.child_identity_or_none(target_name) != expected_identity:
            raise RecoveryTargetExecutionError(
                "restored target pathname identity changed after reverse transition"
            )
        return True
    except Exception as exc:
        raise RecoveryTargetExecutionError(
            "suspect reverse rename occurred but final restored-byte proof failed; "
            "attempt remains unresolved"
        ) from exc
    finally:
        if fresh_target_fd >= 0:
            try:
                os.close(fresh_target_fd)
            except OSError:
                pass


def _execute_windows_replace(plan: RecoveryTargetExecutionPlan) -> _PhysicalOutcome:
    if os.name != "nt":
        raise RecoveryTargetExecutionError("existing-target execution requires native Windows")
    target = Path(plan.target_path)
    root, parent = _bind_final_topology(plan)
    if not isinstance(parent, WindowsDirectoryPin):
        parent.close(); root.close()
        raise RecoveryTargetExecutionError("native Windows target-parent authority is unavailable")

    target_fd = -1
    parked = False
    installed = False
    suspect_name = f".{target.name}.ppa-recovery-{plan.execution_id}.suspect"
    suspect_path = target.parent / suspect_name
    original: _FileSnapshot | None = None
    try:
        if parent.child_info_or_none(suspect_name) is not None:
            raise RecoveryTargetExecutionError("deterministic recovery suspect-retention name is already occupied")
        target_fd = _open_windows_target_fd(parent, target.name)
        original = _hash_fd_snapshot(
            target_fd,
            expected_sha256=plan.target_initial_sha256,
            expected_size=plan.target_initial_size_bytes,
            expected_identity=(
                int(plan.target_initial_fs_device_id or -1), int(plan.target_initial_fs_object_id or -1)
            ),
            expected_mtime_ns=plan.target_initial_mtime_ns,
            label="exact suspect target",
        )
        parent.rename_fd(target_fd, suspect_name, replace=False)
        parked = True
        if parent.child_info_or_none(target.name) is not None:
            raise RecoveryTargetExecutionError("target pathname remained occupied after exact-handle parking")
        parked_snapshot = _hash_fd_snapshot(
            target_fd,
            expected_sha256=original.sha256,
            expected_size=original.size_bytes,
            expected_identity=(int(original.fs_device_id), int(original.fs_object_id)),
            expected_mtime_ns=original.mtime_ns,
            label="parked suspect target",
        )

        temp_identity: tuple[int, int] | None = None
        with BoundTemporaryFile.create(
            target.parent,
            prefix=f".{target.name}.ppa-recovery-{plan.execution_id[:8]}-",
            suffix=".tmp",
            expected_parent_identity=(
                int(plan.target_parent_fs_device_id), int(plan.target_parent_fs_object_id)
            ),
        ) as temp:
            _copy_donor_to_temp(plan, temp)
            temp_identity = tuple(temp.identity)
            temp.install(target, replace=False)
            installed = True

        assert temp_identity is not None
        snap = _verify_installed_path(
            target,
            expected_sha256=plan.expected_sha256,
            expected_size=plan.donor_materialized_size_bytes,
            expected_identity=temp_identity,
        )
        # Re-attest retained suspect on the same handle after donor installation.
        parked_snapshot = _hash_fd_snapshot(
            target_fd,
            expected_sha256=original.sha256,
            expected_size=original.size_bytes,
            expected_identity=(int(original.fs_device_id), int(original.fs_object_id)),
            expected_mtime_ns=original.mtime_ns,
            label="retained suspect target",
        )
        root.verify_pathname()
        parent.verify_pathname()
        return _PhysicalOutcome(
            result_state=RESULT_PLACED,
            installed=snap,
            suspect_path=str(suspect_path),
            suspect=parked_snapshot,
            source_namespace_changed=True,
            detail="expected target placed and exact displaced suspect retained",
        )
    except BaseException as exc:
        if parked and not installed and target_fd >= 0 and original is not None:
            if _restore_parked_windows_target(
                parent,
                fd=target_fd,
                parked_name=suspect_name,
                target_name=target.name,
                original=original,
            ):
                parked = False
                if isinstance(exc, (RecoveryTargetExecutionError, SecureWriteError, OSError)):
                    return _PhysicalOutcome(
                        result_state=RESULT_RESTORED,
                        installed=None,
                        suspect_path=None,
                        suspect=original,
                        source_namespace_changed=False,
                        detail=f"execution aborted and exact suspect target was restored: {exc}",
                    )
        if installed:
            raise RecoveryTargetExecutionError(
                "target namespace changed but final execution checkpoint could not be proven; attempt remains unresolved"
            ) from exc
        if parked:
            raise RecoveryTargetExecutionError(
                f"exact suspect remains parked at {suspect_path}; attempt remains unresolved for manual review"
            ) from exc
        if isinstance(exc, RecoveryTargetExecutionError):
            return _PhysicalOutcome(
                result_state=RESULT_ABORTED,
                installed=None,
                suspect_path=None,
                suspect=None,
                source_namespace_changed=False,
                detail=f"execution aborted before target transition: {exc}",
            )
        raise
    finally:
        if target_fd >= 0:
            try:
                os.close(target_fd)
            except OSError:
                pass
        parent.close()
        root.close()


def _physical_execute(plan: RecoveryTargetExecutionPlan) -> _PhysicalOutcome:
    if plan.replacement_mode == MODE_RESTORE:
        return _execute_missing_restore(plan)
    if plan.replacement_mode == MODE_REPLACE:
        return _execute_windows_replace(plan)
    raise RecoveryTargetExecutionError("unknown target replacement mode")


def _result_fingerprint(plan: RecoveryTargetExecutionPlan, outcome: _PhysicalOutcome, completed_at: str) -> str:
    return _fingerprint({
        "execution_id": plan.execution_id,
        "readiness_id": plan.readiness_id,
        "file_id": plan.file_id,
        "result_state": outcome.result_state,
        "installed": None if outcome.installed is None else asdict(outcome.installed),
        "suspect_path": outcome.suspect_path,
        "suspect": None if outcome.suspect is None else asdict(outcome.suspect),
        "source_namespace_changed": outcome.source_namespace_changed,
        "verify_reconciliation_required": outcome.result_state == RESULT_PLACED,
        "completed_at": completed_at,
    })


def _record_result(
    conn: Connection,
    plan: RecoveryTargetExecutionPlan,
    outcome: _PhysicalOutcome,
) -> RecoveryTargetExecutionResult:
    completed_at = _now()
    result_id = str(uuid.uuid4())
    fp = _result_fingerprint(plan, outcome, completed_at)
    installed = outcome.installed
    suspect = outcome.suspect
    verify_required = outcome.result_state == RESULT_PLACED
    conn.execute(
        """
        INSERT INTO archive_recovery_target_execution_results(
            result_id,execution_id,readiness_id,target_file_id,result_state,
            installed_sha256,installed_size_bytes,installed_fs_device_id,installed_fs_object_id,
            installed_link_count,suspect_retained_path,suspect_sha256,suspect_size_bytes,
            suspect_fs_device_id,suspect_fs_object_id,source_namespace_changed,
            verify_reconciliation_required,evidence_fingerprint,detail,completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            result_id,plan.execution_id,plan.readiness_id,plan.file_id,outcome.result_state,
            None if installed is None else installed.sha256,
            None if installed is None else installed.size_bytes,
            None if installed is None else installed.fs_device_id,
            None if installed is None else installed.fs_object_id,
            None if installed is None else installed.link_count,
            outcome.suspect_path,
            None if suspect is None else suspect.sha256,
            None if suspect is None else suspect.size_bytes,
            None if suspect is None else suspect.fs_device_id,
            None if suspect is None else suspect.fs_object_id,
            1 if outcome.source_namespace_changed else 0,
            1 if verify_required else 0,
            fp,outcome.detail,completed_at,
        ),
    )
    event_type = (
        "archive_recovery_target_execution_completed"
        if outcome.result_state == RESULT_PLACED
        else "archive_recovery_target_execution_aborted"
    )
    conn.execute(
        "INSERT INTO integrity_events(file_id,event_type,detail) VALUES (?,?,?)",
        (
            plan.file_id,event_type,
            f"Phase-14.3 execution {plan.execution_id}: {outcome.result_state}. {outcome.detail}",
        ),
    )
    return RecoveryTargetExecutionResult(
        schema=EXECUTION_RESULT_SCHEMA,
        result_id=result_id,
        execution_id=plan.execution_id,
        readiness_id=plan.readiness_id,
        file_id=plan.file_id,
        result_state=outcome.result_state,
        installed_sha256=None if installed is None else installed.sha256,
        installed_size_bytes=None if installed is None else installed.size_bytes,
        installed_fs_device_id=None if installed is None else installed.fs_device_id,
        installed_fs_object_id=None if installed is None else installed.fs_object_id,
        installed_link_count=None if installed is None else installed.link_count,
        suspect_retained_path=outcome.suspect_path,
        suspect_sha256=None if suspect is None else suspect.sha256,
        suspect_size_bytes=None if suspect is None else suspect.size_bytes,
        suspect_fs_device_id=None if suspect is None else suspect.fs_device_id,
        suspect_fs_object_id=None if suspect is None else suspect.fs_object_id,
        source_namespace_changed=outcome.source_namespace_changed,
        verify_reconciliation_required=verify_required,
        evidence_fingerprint=fp,
        detail=outcome.detail,
        completed_at=completed_at,
    )


def execute_target_replacement(
    conn: Connection,
    plan: RecoveryTargetExecutionPlan,
    *,
    confirmation: str,
    note: str | None = None,
) -> RecoveryTargetExecutionResult:
    """Consume one exact reviewed plan and perform at most one recovery attempt.

    Authorization is committed in its own transaction *before* source mutation.
    A second ``BEGIN IMMEDIATE`` then spans fresh evidence revalidation, the
    physical operation, and immutable result checkpoint.  A hard crash may leave
    an attempt without a result; such an unresolved attempt blocks replay.
    """
    rebuilt, _attempt = _authorize_attempt(conn, plan, confirmation=confirmation, note=note)

    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM archive_recovery_target_execution_results WHERE execution_id=?",
            (rebuilt.execution_id,),
        ).fetchone() is not None:
            raise RecoveryTargetExecutionError("execution attempt already has an immutable result")

        # One last full Phase-14.2 reconstruction under the write lock.  The
        # public builder rejects an existing attempt, so explicitly allow only
        # this exact already-authorized execution ID.
        try:
            current = _build_plan(
                conn,
                readiness_id=rebuilt.readiness_id,
                execution_id=rebuilt.execution_id,
                allow_existing_execution_id=rebuilt.execution_id,
            )
        except RecoveryTargetExecutionError as exc:
            outcome = _PhysicalOutcome(
                result_state=RESULT_ABORTED,
                installed=None,
                suspect_path=None,
                suspect=None,
                source_namespace_changed=False,
                detail=f"fresh execution revalidation failed before target transition: {exc}",
            )
            result = _record_result(conn, rebuilt, outcome)
            conn.commit()
            return result
        if current.execution_plan_fingerprint != rebuilt.execution_plan_fingerprint:
            outcome = _PhysicalOutcome(
                result_state=RESULT_ABORTED,
                installed=None,
                suspect_path=None,
                suspect=None,
                source_namespace_changed=False,
                detail="fresh execution evidence no longer matches the confirmed preview",
            )
            result = _record_result(conn, rebuilt, outcome)
            conn.commit()
            return result

        outcome = _physical_execute(current)
        result = _record_result(conn, current, outcome)
        conn.commit()
        return result
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def inspect_recovery_execution_status(conn: Connection, *, execution_id: str) -> RecoveryExecutionStatus:
    """Read-only inspection for durable/resumable crash semantics.

    This never declares an unresolved attempt successful and never grants a new
    attempt.  It exposes whether the immutable result checkpoint exists and the
    current target/suspect state so manual reconciliation can be reviewed.
    """
    eid = _validated_uuid(execution_id, label="execution ID")
    attempt = conn.execute(
        "SELECT * FROM archive_recovery_target_execution_attempts WHERE execution_id=?",
        (eid,),
    ).fetchone()
    if attempt is None:
        raise RecoveryTargetExecutionError("unknown recovery execution attempt")
    result = conn.execute(
        "SELECT * FROM archive_recovery_target_execution_results WHERE execution_id=?",
        (eid,),
    ).fetchone()

    target = Path(str(attempt["target_path"]))
    state = "missing"
    sha = None
    if target.exists() and not target.is_symlink():
        try:
            fd = _open_regular_path_fd(target)
            try:
                snap = _hash_fd_snapshot(fd, label="current recovery target")
                sha = snap.sha256
                state = "matches_expected" if sha == str(attempt["expected_sha256"]) else "present_other"
            finally:
                os.close(fd)
        except RecoveryTargetExecutionError:
            state = "unreadable_or_unsafe"

    suspect_path = None
    if os.name == "nt" and str(attempt["replacement_mode"]) == MODE_REPLACE:
        candidate = target.parent / f".{target.name}.ppa-recovery-{eid}.suspect"
        if candidate.exists() or candidate.is_symlink():
            suspect_path = str(candidate)

    if result is not None:
        detail = f"immutable result recorded: {result['result_state']}"
        resolved = True
        result_state = str(result["result_state"])
    else:
        detail = "authorized attempt has no immutable result; automatic replay is blocked and manual review is required"
        resolved = False
        result_state = None
    return RecoveryExecutionStatus(
        execution_id=eid,
        readiness_id=str(attempt["readiness_id"]),
        file_id=str(attempt["target_file_id"]),
        resolved=resolved,
        result_state=result_state,
        target_state=state,
        target_sha256=sha,
        suspect_retained_path=suspect_path,
        detail=detail,
    )


def concise_execution_plan_text(plan: RecoveryTargetExecutionPlan) -> str:
    return "\n".join([
        "Phase 14.3 — Target-Replacement Execution Preview",
        f"Execution ID: {plan.execution_id}",
        f"Readiness ID: {plan.readiness_id}",
        f"Target: {plan.file_id}  {plan.target_path}",
        f"Mode: {plan.replacement_mode}",
        f"Expected SHA-256: {plan.expected_sha256}",
        "Target replacement authorised by preview: NO",
        "Recovery execution authorised by preview: NO",
        "",
        "To consume this exact one-attempt preview, repeat BOTH:",
        f"  --execution-id {plan.execution_id}",
        f"  --confirm \"{plan.confirmation_phrase}\"",
    ])


def concise_execution_result_text(result: RecoveryTargetExecutionResult) -> str:
    lines = [
        "Phase 14.3 — Target-Replacement Execution Result",
        f"Execution ID: {result.execution_id}",
        f"Result: {result.result_state}",
    ]
    if result.installed_sha256:
        lines.append(f"Installed SHA-256: {result.installed_sha256}")
    if result.suspect_retained_path:
        lines.append(f"Retained suspect: {result.suspect_retained_path}")
    if result.verify_reconciliation_required:
        lines.append("Catalogue health reconciliation: REQUIRED via ordinary Verify")
    if result.detail:
        lines.append(f"Detail: {result.detail}")
    return "\n".join(lines)
