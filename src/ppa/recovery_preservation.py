"""Phase 14.0 — recovery execution protocol and suspect-byte preservation staging.

This is the first recovery slice allowed to write bytes *derived from* a source
photograph, but it still does not restore, replace, rename, move, delete or
otherwise modify any source photograph.  A successful stage copies the exact
currently-suspect target bytes into PPA operational storage after revalidating a
frozen Phase-13 proposal, then proves the preservation copy byte-for-byte.

The donor remains read-only and is never materialised in Phase 14.0.  The target
remains read-only and is never replaced.  Missing targets produce an audited
"no suspect bytes to preserve" checkpoint instead of inventing evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from sqlite3 import Connection
import stat
import uuid

from ppa.hashing import sha256_file
from ppa.physical_observation import PhysicalObservationError, StableFileObservation, observe_stable_image
from ppa.recovery_planning import RecoveryPlanningError, build_recovery_plan
from ppa.secure_write import (
    BoundDirectory, BoundTemporaryFile, SecureWriteError, atomic_write_bytes,
    ensure_directory_authority, is_windows_reparse_point_stat,
    windows_path_has_reparse_component,
)
from ppa.source_tree_authority import SourceTreeAuthorityError, SourceTreeAuthorityPolicy
from ppa.operational_authority import OperationalAuthorityError, require_directory

PRESERVATION_PLAN_SCHEMA = "ppa-recovery-preservation-plan/1"
PRESERVATION_RESULT_SCHEMA = "ppa-recovery-preservation-stage/1"
PRESERVATION_MANIFEST_SCHEMA = "ppa-recovery-preservation-manifest/1"

STAGE_PRESERVED = "suspect_bytes_preserved"
STAGE_MISSING = "target_missing_no_preservation_required"

# Small reserve so a preservation copy never intentionally consumes the last
# available bytes on the operational filesystem.  This is a guardrail, not an
# exact filesystem-allocation model.
_MIN_FREE_RESERVE_BYTES = 8 * 1024 * 1024


class RecoveryPreservationError(ValueError):
    """Phase-14 preservation staging cannot proceed from current evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


def _db_path(conn: Connection) -> Path:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(row[2]).expanduser().resolve(strict=False)
    raise RecoveryPreservationError("preservation staging requires a file-backed catalogue")


def default_preservation_root(conn: Connection) -> Path:
    return _db_path(conn).parent / "recovery-preservation"


def _library_roots(conn: Connection) -> tuple[str, ...]:
    rows = conn.execute("SELECT root_canonical_path FROM libraries ORDER BY id").fetchall()
    return tuple(_canonical(Path(row[0])) for row in rows if row[0])


def _validate_bound_preservation_root(
    authority, root: Path, conn: Connection, source_policy: SourceTreeAuthorityPolicy
) -> None:
    """Validate the exact already-bound operational root object."""
    try:
        source_policy.validate_authority(authority, purpose="recovery preservation storage")
    except SourceTreeAuthorityError as exc:
        raise RecoveryPreservationError(str(exc)) from exc
    _validate_preservation_root(conn, root)
    authority.verify_pathname()


def _validate_preservation_root(conn: Connection, root: Path) -> Path:
    """Return an absolute operational root that cannot resolve into source data."""
    root = Path(root).expanduser()
    absolute = root if root.is_absolute() else Path.cwd() / root
    canonical = _canonical(absolute)
    for library_root in _library_roots(conn):
        if canonical == library_root or _within(canonical, library_root):
            raise RecoveryPreservationError(
                "recovery preservation storage resolves inside a registered source Library"
            )

    db = _db_path(conn)
    db_canonical = _canonical(db)
    if canonical == db_canonical:
        raise RecoveryPreservationError("recovery preservation storage collides with the catalogue database")

    # The dedicated root may exist only as a real directory.  Refusing a leaf
    # symlink keeps the operational store's identity simple and auditable.
    if windows_path_has_reparse_component(absolute):
        raise RecoveryPreservationError(
            "recovery preservation storage traverses a Windows junction or reparse point"
        )
    if absolute.exists():
        try:
            root_st = absolute.lstat()
        except OSError as exc:
            raise RecoveryPreservationError("recovery preservation root cannot be inspected safely") from exc
        if stat.S_ISLNK(root_st.st_mode) or is_windows_reparse_point_stat(root_st):
            raise RecoveryPreservationError(
                "recovery preservation root may not be a symbolic link, junction, or reparse point"
            )
        if not stat.S_ISDIR(root_st.st_mode):
            raise RecoveryPreservationError("recovery preservation root is not a directory")
    return absolute.resolve(strict=False)


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _free_bytes_for(path: Path) -> int:
    return int(shutil.disk_usage(_nearest_existing(path)).free)


def _proposal_row(conn: Connection, proposal_id: str):
    return conn.execute(
        "SELECT * FROM archive_recovery_plan_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()


def _observation_payload(observation: StableFileObservation) -> dict:
    return {
        "state": observation.state,
        "sha256": observation.sha256,
        "size_bytes": observation.size_bytes,
        "mtime_ns": observation.mtime_ns,
        "fs_device_id": observation.fs_device_id,
        "fs_object_id": observation.fs_object_id,
        "width_px": observation.width_px,
        "height_px": observation.height_px,
        "mime_type": observation.mime_type,
    }


def _same_observation(a: StableFileObservation, b: StableFileObservation) -> bool:
    return _observation_payload(a) == _observation_payload(b)


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validated_stage_id(stage_id: str | None) -> str:
    """Return one canonical UUID stage identifier.

    Stage identifiers are filesystem path components at the first recovery write
    boundary, so accepting arbitrary caller text would turn a logical ID into
    path authority (for example ``../library`` or an absolute path).  Phase 14
    stage IDs are intentionally opaque UUIDs and are never user-facing paths.
    """
    if stage_id is None:
        return str(uuid.uuid4())
    value = str(stage_id).strip()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RecoveryPreservationError("preservation stage ID must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise RecoveryPreservationError("preservation stage ID must be a canonical UUID")
    return canonical


def _directory_identity(path: Path) -> tuple[int, int]:
    if windows_path_has_reparse_component(path):
        raise RecoveryPreservationError("preservation stage directory is unsafe because it traverses a Windows reparse point")
    try:
        st = path.lstat()
    except OSError as exc:
        raise RecoveryPreservationError("preservation stage directory disappeared") from exc
    if (
        stat.S_ISLNK(st.st_mode)
        or is_windows_reparse_point_stat(st)
        or not stat.S_ISDIR(st.st_mode)
    ):
        raise RecoveryPreservationError("preservation stage directory identity is unsafe")
    return int(st.st_dev), int(st.st_ino)


def _regular_file_identity(path: Path) -> tuple[int, int]:
    if windows_path_has_reparse_component(path):
        raise RecoveryPreservationError("preservation evidence path traverses a Windows reparse point")
    try:
        st = path.lstat()
    except OSError as exc:
        raise RecoveryPreservationError("preservation evidence file disappeared") from exc
    if (
        stat.S_ISLNK(st.st_mode)
        or is_windows_reparse_point_stat(st)
        or not stat.S_ISREG(st.st_mode)
    ):
        raise RecoveryPreservationError("preservation evidence file identity is unsafe")
    return int(st.st_dev), int(st.st_ino)


def _attest_single_link_evidence(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    expected_sha256: str,
    expected_size: int | None = None,
    label: str = "recovery evidence",
) -> tuple[int, int]:
    """Re-attest the exact evidence object immediately before checkpoint commit.

    Phase 14.1.17.4 treats single-link topology as part of evidence authority.
    The file is opened without following symlinks where the platform supports
    that flag, then identity, regular-file type and link count are checked both
    before and after descriptor-bound hashing.  A late hard-link therefore
    invalidates the checkpoint instead of gaining a post-commit metadata write
    through a source-Library alias.
    """
    if windows_path_has_reparse_component(path):
        raise RecoveryPreservationError(f"{label} traverses a Windows reparse point")
    # Descriptor hashing must be byte-exact on every platform.  On Windows,
    # CRT file descriptors default to text mode unless O_BINARY is supplied;
    # os.read() can otherwise translate CRLF / honour CTRL-Z and make a valid
    # binary evidence file appear to have changed size or content.
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RecoveryPreservationError(f"{label} disappeared before checkpoint commit") from exc
    try:
        before = os.fstat(fd)
        identity = (int(before.st_dev), int(before.st_ino))
        if not stat.S_ISREG(before.st_mode) or is_windows_reparse_point_stat(before):
            raise RecoveryPreservationError(f"{label} is not a safe regular file")
        if identity != tuple(expected_identity):
            raise RecoveryPreservationError(f"{label} filesystem identity changed before checkpoint commit")
        if int(getattr(before, "st_nlink", 1) or 1) != 1:
            raise RecoveryPreservationError(f"{label} gained a hard-link alias before checkpoint commit")

        digest = hashlib.sha256()
        size = 0
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)

        after = os.fstat(fd)
        after_identity = (int(after.st_dev), int(after.st_ino))
        if after_identity != identity:
            raise RecoveryPreservationError(f"{label} filesystem identity changed during final attestation")
        if int(getattr(after, "st_nlink", 1) or 1) != 1:
            raise RecoveryPreservationError(f"{label} gained a hard-link alias during final attestation")
        if expected_size is not None and size != int(expected_size):
            raise RecoveryPreservationError(f"{label} changed before checkpoint commit (size mismatch)")
        if digest.hexdigest() != str(expected_sha256):
            raise RecoveryPreservationError(f"{label} changed before checkpoint commit (content mismatch)")
        return identity
    finally:
        os.close(fd)


def _safe_cleanup_created_stage(
    bound_stage: BoundDirectory | None,
    *,
    owned_paths: set[Path],
) -> None:
    """Retain failed POSIX preservation stages instead of deleting by child name.

    Binding the stage directory protects where observation occurs, but it does
    not provide exact-object authority for ``unlink(child-name)`` or for a later
    ``rmdir(stage-name)``.  Either namespace slot can be substituted after an
    identity check.  Phase 14.1.17 therefore treats failed-stage debris as a
    manual-recovery artifact on POSIX.
    """
    if bound_stage is None:
        return
    bound_stage.verify_handle()
    for owned in tuple(owned_paths):
        try:
            if owned.parent == bound_stage.path:
                BoundDirectory.validate_child_name(owned.name)
        except (SecureWriteError, ValueError):
            continue
    # Intentionally do not unlink children or rmdir the stage.  Debris is
    # recoverable; deleting a substituted source object is not.


@dataclass(frozen=True)
class RecoveryPreservationPlan:
    schema: str
    stage_id: str
    proposal_id: str
    phase13_evidence_fingerprint: str
    file_id: str
    photo_id: str
    library_id: int
    target_path: str
    donor_file_id: str
    donor_path: str
    expected_revision_id: str
    expected_sha256: str
    recovery_intent_resolution_id: str
    target_state: str
    target_observed_sha256: str | None
    target_size_bytes: int | None
    target_mtime_ns: int | None
    target_fs_device_id: str | None
    target_fs_object_id: str | None
    donor_observed_sha256: str
    donor_size_bytes: int
    donor_mtime_ns: int
    donor_fs_device_id: str | None
    donor_fs_object_id: str | None
    preservation_required: bool
    preservation_root: str
    preservation_path: str | None
    manifest_path: str
    required_bytes: int
    available_bytes: int
    execution_authorized: bool
    target_replacement_authorized: bool
    donor_materialization_authorized: bool
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
class RecoveryPreservationResult:
    schema: str
    stage_id: str
    proposal_id: str
    stage_state: str
    preservation_path: str | None
    preserved_sha256: str | None
    preserved_size_bytes: int | None
    manifest_path: str
    manifest_sha256: str
    target_replacement_performed: bool
    donor_materialized: bool
    staged_at: str
    evidence_fingerprint: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )


def build_preservation_plan(
    conn: Connection,
    *,
    proposal_id: str,
    stage_id: str | None = None,
    preservation_root: str | Path | None = None,
) -> RecoveryPreservationPlan:
    """Revalidate one recorded Phase-13 proposal and plan preservation only.

    Read-only with respect to both source files and operational staging storage.
    No directory or preservation file is created by this function.
    """
    row = _proposal_row(conn, proposal_id)
    if row is None:
        raise RecoveryPreservationError("unknown recorded recovery proposal")
    if row["proposal_state"] != "dry_run_not_executed":
        raise RecoveryPreservationError("recovery proposal is not in the frozen dry-run state")
    if conn.execute(
        "SELECT 1 FROM archive_recovery_preservation_stages WHERE proposal_id=? LIMIT 1",
        (proposal_id,),
    ).fetchone() is not None:
        raise RecoveryPreservationError(
            "this recovery proposal already has a preservation stage; obtain a fresh reviewed proposal before staging again"
        )

    try:
        rebuilt = build_recovery_plan(
            conn,
            file_id=str(row["target_file_id"]),
            donor_file_id=str(row["donor_file_id"]),
            proposal_id=str(row["proposal_id"]),
        )
    except RecoveryPlanningError as exc:
        raise RecoveryPreservationError(
            "recorded recovery proposal is no longer executable as reviewed; refresh recovery planning"
        ) from exc
    if rebuilt.evidence_fingerprint != row["evidence_fingerprint"]:
        raise RecoveryPreservationError(
            "recorded recovery proposal is stale: current target/donor evidence no longer matches the reviewed proposal"
        )
    if rebuilt.recovery_intent_resolution_id != row["recovery_intent_resolution_id"]:
        raise RecoveryPreservationError("human recovery intent changed; refresh recovery planning")

    # Build one new Phase-14 evidence boundary from fresh observations.  The
    # Phase-13 builder already re-attested both sides; re-read donor here so this
    # plan independently binds the execution-stage evidence it is about to show.
    try:
        donor = observe_stable_image(Path(rebuilt.donor_path), expected_sha256=rebuilt.expected_sha256)
        target = observe_stable_image(Path(rebuilt.target_path), expected_sha256=rebuilt.expected_sha256)
    except PhysicalObservationError as exc:
        raise RecoveryPreservationError(
            "physical File changed while preservation readiness was being observed"
        ) from exc
    if donor.state != "matches_expected" or donor.sha256 != rebuilt.expected_sha256:
        raise RecoveryPreservationError("donor no longer reproduces the immutable expected revision")
    if target.state == "matches_expected":
        raise RecoveryPreservationError("target already reproduces expected bytes; run Verify instead of staging recovery")
    if target.state not in {"still_mismatched", "unreadable", "missing"}:
        raise RecoveryPreservationError(f"target state {target.state!r} is not eligible for preservation staging")

    root = _validate_preservation_root(
        conn,
        Path(preservation_root) if preservation_root is not None else default_preservation_root(conn),
    )
    sid = _validated_stage_id(stage_id)
    stage_dir = root / sid
    suffix = Path(rebuilt.target_path).suffix or ".bin"
    preservation_path = None if target.state == "missing" else str(stage_dir / ("suspect-source" + suffix))
    manifest_path = str(stage_dir / "manifest.json")
    required = int(target.size_bytes or 0)
    available = _free_bytes_for(root)
    # Keep enough free space for the exact suspect bytes plus manifest/temp-file
    # overhead and a small safety reserve.
    if target.state != "missing" and available < required + _MIN_FREE_RESERVE_BYTES:
        raise RecoveryPreservationError(
            f"insufficient operational storage for preservation staging: need at least "
            f"{required + _MIN_FREE_RESERVE_BYTES} bytes free, observed {available}"
        )

    evidence = {
        "stage_id": sid,
        "proposal_id": str(row["proposal_id"]),
        "phase13_evidence_fingerprint": str(row["evidence_fingerprint"]),
        "target": {
            "file_id": rebuilt.file_id,
            "photo_id": rebuilt.photo_id,
            "library_id": rebuilt.library_id,
            "path": rebuilt.target_path,
            "expected_revision_id": rebuilt.expected_revision_id,
            "expected_sha256": rebuilt.expected_sha256,
            "recovery_intent_resolution_id": rebuilt.recovery_intent_resolution_id,
            **_observation_payload(target),
        },
        "donor": {
            "file_id": rebuilt.donor_file_id,
            "path": rebuilt.donor_path,
            **_observation_payload(donor),
        },
        "preservation_required": target.state != "missing",
        "preservation_root": str(root),
        "preservation_path": preservation_path,
        "manifest_path": manifest_path,
        "execution_authorized": False,
        "target_replacement_authorized": False,
        "donor_materialization_authorized": False,
    }
    return RecoveryPreservationPlan(
        schema=PRESERVATION_PLAN_SCHEMA,
        stage_id=sid,
        proposal_id=str(row["proposal_id"]),
        phase13_evidence_fingerprint=str(row["evidence_fingerprint"]),
        file_id=rebuilt.file_id,
        photo_id=rebuilt.photo_id,
        library_id=rebuilt.library_id,
        target_path=rebuilt.target_path,
        donor_file_id=rebuilt.donor_file_id,
        donor_path=rebuilt.donor_path,
        expected_revision_id=rebuilt.expected_revision_id,
        expected_sha256=rebuilt.expected_sha256,
        recovery_intent_resolution_id=rebuilt.recovery_intent_resolution_id,
        target_state=target.state,
        target_observed_sha256=target.sha256,
        target_size_bytes=target.size_bytes,
        target_mtime_ns=target.mtime_ns,
        target_fs_device_id=target.fs_device_id,
        target_fs_object_id=target.fs_object_id,
        donor_observed_sha256=str(donor.sha256),
        donor_size_bytes=int(donor.size_bytes or 0),
        donor_mtime_ns=int(donor.mtime_ns or 0),
        donor_fs_device_id=donor.fs_device_id,
        donor_fs_object_id=donor.fs_object_id,
        preservation_required=target.state != "missing",
        preservation_root=str(root),
        preservation_path=preservation_path,
        manifest_path=manifest_path,
        required_bytes=required,
        available_bytes=available,
        execution_authorized=False,
        target_replacement_authorized=False,
        donor_materialization_authorized=False,
        evidence_fingerprint=_fingerprint(evidence),
    )


def _copy_preserved_bytes(source: Path, temporary: BoundTemporaryFile) -> tuple[str, int]:
    """Copy source bytes through the exact descriptor-bound temporary object."""
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as src, temporary.binary_writer() as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            dst.write(chunk)
            total += len(chunk)
    temporary.sync_and_verify()
    return digest.hexdigest(), total


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability; Windows does not expose portable fsync."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_manifest(
    path: Path, payload: dict, expected_parent_identity: tuple[int, int] | None = None
) -> str:
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        atomic_write_bytes(
            path, raw, prefix="manifest.", suffix=".tmp", replace=True,
            expected_parent_identity=expected_parent_identity,
        )
    except SecureWriteError as exc:
        raise RecoveryPreservationError("preservation manifest temporary identity changed") from exc
    return hashlib.sha256(raw).hexdigest()


def _preservation_checkpoint_is_durable(conn: Connection, stage_id: str | None) -> bool:
    """Return whether the stage checkpoint is already committed.

    This helper is used only from exception handling.  If this connection is
    still inside a transaction, its INSERT is not durable and must not be
    mistaken for committed authority merely because the connection can read its
    own uncommitted row.  Once ``in_transaction`` is false, a visible checkpoint
    is committed catalogue state and filesystem rollback cleanup must stop.
    """
    if stage_id is None or conn.in_transaction:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM archive_recovery_preservation_stages WHERE stage_id=? LIMIT 1",
            (stage_id,),
        ).fetchone() is not None
    except Exception:
        # Ambiguity must preserve evidence, not destroy it.  If transaction state
        # says commit may already have completed but the checkpoint cannot be
        # queried safely, fail closed by withholding cleanup.
        return True


def execute_preservation_stage(
    conn: Connection,
    plan: RecoveryPreservationPlan,
    *,
    note: str | None = None,
) -> RecoveryPreservationResult:
    """Preserve suspect target bytes into operational storage, then stop.

    This function never writes donor bytes to staging and never writes any bytes
    to the target source path.  All current evidence is rebuilt under
    ``BEGIN IMMEDIATE``.  Source and donor are re-attested after staging and any
    mismatch rolls the database transaction back and removes the stage directory.
    """
    if plan.schema != PRESERVATION_PLAN_SCHEMA:
        raise RecoveryPreservationError("invalid preservation plan schema")
    if plan.execution_authorized or plan.target_replacement_authorized or plan.donor_materialization_authorized:
        raise RecoveryPreservationError("Phase 14.0 plan contains forbidden recovery authority")
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise RecoveryPreservationError("preservation-stage note is too long")

    stage_dir: Path | None = None
    stage_root: Path | None = None
    stage_dir_identity: tuple[int, int] | None = None
    root_authority = None
    stage_authority = None
    bound_stage: BoundDirectory | None = None
    owned_stage_paths: set[Path] = set()
    checkpoint_stage_id: str | None = None
    checkpoint_committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        rebuilt = build_preservation_plan(
            conn,
            proposal_id=plan.proposal_id,
            stage_id=plan.stage_id,
            preservation_root=plan.preservation_root,
        )
        if rebuilt.evidence_fingerprint != plan.evidence_fingerprint:
            raise RecoveryPreservationError(
                "preservation plan is stale: physical/catalogue evidence changed; rebuild the stage plan"
            )
        if rebuilt.phase13_evidence_fingerprint != plan.phase13_evidence_fingerprint:
            raise RecoveryPreservationError("Phase-13 recovery proposal evidence changed")
        checkpoint_stage_id = rebuilt.stage_id

        try:
            source_policy = SourceTreeAuthorityPolicy.from_connection(conn)
        except SourceTreeAuthorityError as exc:
            raise RecoveryPreservationError(str(exc)) from exc
        root = _validate_preservation_root(conn, Path(plan.preservation_root))
        # Bootstrap authority by selecting/pinning the root object first and
        # validating THAT exact object before it can create the stage namespace.
        try:
            root_authority = ensure_directory_authority(
                root,
                validator=lambda authority: _validate_bound_preservation_root(
                    authority, root, conn, source_policy
                ),
            )
        except (OSError, SecureWriteError) as exc:
            raise RecoveryPreservationError(
                "could not establish safe preservation-root authority"
            ) from exc
        try:
            require_directory(conn, "recovery_preservation", root_authority)
        except OperationalAuthorityError as exc:
            root_authority.close()
            raise RecoveryPreservationError(str(exc)) from exc
        stage_root = root

        # Revalidate even caller-supplied plans before using the identifier as a
        # child component.  The child directory is created *relative to the
        # already-validated root authority*, never by Path.mkdir().
        stage_id = _validated_stage_id(plan.stage_id)
        if stage_id != rebuilt.stage_id:
            raise RecoveryPreservationError("preservation stage ID changed while rebuilding the plan")
        stage_dir = root / stage_id
        try:
            stage_authority = root_authority.create_directory_child(stage_id)
        except (OSError, SecureWriteError) as exc:
            raise RecoveryPreservationError(
                "preservation stage destination already exists or could not be created safely"
            ) from exc
        stage_dir_identity = tuple(stage_authority.identity)
        if isinstance(stage_authority, BoundDirectory):
            # POSIX rollback may mutate only through this exact descriptor.
            bound_stage = stage_authority

        # Persist/check the exact authority objects before any source-derived
        # bytes are copied.
        _fsync_directory(root)
        try:
            _validate_bound_preservation_root(root_authority, root, conn, source_policy)
            stage_authority.verify_pathname()
        except SecureWriteError as exc:
            raise RecoveryPreservationError(
                "preservation root/stage authority changed during bound stage creation"
            ) from exc
        if tuple(stage_authority.identity) != stage_dir_identity:
            raise RecoveryPreservationError("preservation stage directory authority changed after creation")

        # Re-check free space after creating the operational directory and before
        # copying any source-derived bytes.
        free_now = _free_bytes_for(stage_dir)
        if rebuilt.preservation_required and free_now < rebuilt.required_bytes + _MIN_FREE_RESERVE_BYTES:
            raise RecoveryPreservationError("insufficient operational storage at preservation execution time")

        preserved_sha: str | None = None
        preserved_size: int | None = None
        preservation_identity: tuple[int, int] | None = None
        preservation_path = Path(rebuilt.preservation_path) if rebuilt.preservation_path else None

        # Fresh pre-copy observations after the stage directory exists.
        try:
            target_before = observe_stable_image(Path(rebuilt.target_path), expected_sha256=rebuilt.expected_sha256)
            donor_before = observe_stable_image(Path(rebuilt.donor_path), expected_sha256=rebuilt.expected_sha256)
        except PhysicalObservationError as exc:
            raise RecoveryPreservationError("source changed at the preservation execution boundary") from exc
        if donor_before.state != "matches_expected" or donor_before.sha256 != rebuilt.expected_sha256:
            raise RecoveryPreservationError("donor changed before preservation staging")
        if target_before.state != rebuilt.target_state or target_before.sha256 != rebuilt.target_observed_sha256:
            raise RecoveryPreservationError("target changed before preservation staging")

        if rebuilt.preservation_required:
            assert preservation_path is not None
            try:
                temp = BoundTemporaryFile.create(
                    stage_dir, prefix="suspect-source.", suffix=".pending",
                    expected_parent_identity=stage_dir_identity,
                )
            except SecureWriteError as exc:
                raise RecoveryPreservationError(
                    "preservation stage write authority changed before temporary creation"
                ) from exc
            try:
                try:
                    copied_sha, copied_size = _copy_preserved_bytes(Path(rebuilt.target_path), temp)
                except SecureWriteError as exc:
                    raise RecoveryPreservationError(
                        "preservation temporary identity changed while suspect bytes were copied"
                    ) from exc
                if copied_sha != rebuilt.target_observed_sha256 or copied_size != rebuilt.target_size_bytes:
                    raise RecoveryPreservationError(
                        "target changed while suspect bytes were being preserved; staged bytes discarded"
                    )
                # Independent readback is descriptor-bound too: a substituted
                # pathname can neither receive writes nor become the evidence
                # object we hash.
                readback_sha, readback_size = temp.hash_and_size()
                if readback_sha != copied_sha or readback_size != copied_size:
                    raise RecoveryPreservationError("preservation readback verification failed")

                try:
                    target_after_copy = observe_stable_image(
                        Path(rebuilt.target_path), expected_sha256=rebuilt.expected_sha256
                    )
                except PhysicalObservationError as exc:
                    raise RecoveryPreservationError(
                        "target changed during suspect-byte preservation; staged bytes discarded"
                    ) from exc
                if not _same_observation(target_before, target_after_copy):
                    raise RecoveryPreservationError(
                        "target changed during suspect-byte preservation; staged bytes discarded"
                    )

                try:
                    temp.install(preservation_path, replace=False)
                except SecureWriteError as exc:
                    raise RecoveryPreservationError(
                        "preservation temporary identity/path changed before installation"
                    ) from exc
                owned_stage_paths.add(preservation_path)
                preserved_sha = readback_sha
                preserved_size = copied_size
                preservation_identity = _regular_file_identity(preservation_path)
            finally:
                temp.cleanup()
        else:
            # Missing-target staging is valid only if it remained missing.
            if target_before.state != "missing":
                raise RecoveryPreservationError("target reappeared; refresh recovery planning")

        stage_state = STAGE_PRESERVED if rebuilt.preservation_required else STAGE_MISSING
        staged_at = _now()
        manifest_payload = {
            "schema": PRESERVATION_MANIFEST_SCHEMA,
            "stage_id": rebuilt.stage_id,
            "proposal_id": rebuilt.proposal_id,
            "phase13_evidence_fingerprint": rebuilt.phase13_evidence_fingerprint,
            "phase14_plan_fingerprint": rebuilt.evidence_fingerprint,
            "target": {
                "file_id": rebuilt.file_id,
                "photo_id": rebuilt.photo_id,
                "library_id": rebuilt.library_id,
                "path": rebuilt.target_path,
                "expected_revision_id": rebuilt.expected_revision_id,
                "expected_sha256": rebuilt.expected_sha256,
                "state": rebuilt.target_state,
                "observed_sha256": rebuilt.target_observed_sha256,
                "size_bytes": rebuilt.target_size_bytes,
                "mtime_ns": rebuilt.target_mtime_ns,
                "fs_device_id": rebuilt.target_fs_device_id,
                "fs_object_id": rebuilt.target_fs_object_id,
            },
            "donor": {
                "file_id": rebuilt.donor_file_id,
                "path": rebuilt.donor_path,
                "observed_sha256": rebuilt.donor_observed_sha256,
                "size_bytes": rebuilt.donor_size_bytes,
                "mtime_ns": rebuilt.donor_mtime_ns,
                "fs_device_id": rebuilt.donor_fs_device_id,
                "fs_object_id": rebuilt.donor_fs_object_id,
            },
            "stage_state": stage_state,
            "preservation_path": None if preservation_path is None else str(preservation_path),
            "preserved_sha256": preserved_sha,
            "preserved_size_bytes": preserved_size,
            "target_replacement_performed": False,
            "donor_materialized": False,
            "recovery_execution_authorized": False,
            "staged_at": staged_at,
        }
        manifest_path = Path(rebuilt.manifest_path)
        manifest_sha = _write_manifest(
            manifest_path, manifest_payload, stage_dir_identity
        )
        owned_stage_paths.add(manifest_path)
        manifest_identity = _regular_file_identity(manifest_path)

        # Final physical re-attestation before the catalogue checkpoint commits.
        try:
            target_final = observe_stable_image(Path(rebuilt.target_path), expected_sha256=rebuilt.expected_sha256)
            donor_final = observe_stable_image(Path(rebuilt.donor_path), expected_sha256=rebuilt.expected_sha256)
        except PhysicalObservationError as exc:
            raise RecoveryPreservationError(
                "physical source changed after preservation staging; preservation checkpoint rolled back"
            ) from exc
        if not _same_observation(target_before, target_final):
            raise RecoveryPreservationError(
                "target changed after preservation staging; preservation checkpoint rolled back"
            )
        if not _same_observation(donor_before, donor_final):
            raise RecoveryPreservationError(
                "donor changed after preservation staging; preservation checkpoint rolled back"
            )

        # Final identity/content/link-topology attestation.  This is deliberately
        # performed after source re-observation and immediately before the
        # catalogue checkpoint is constructed.  Single-link status is evidence,
        # not a one-time creation assumption.
        if preservation_path is not None:
            assert preservation_identity is not None and preserved_sha is not None
            _attest_single_link_evidence(
                preservation_path,
                expected_identity=preservation_identity,
                expected_sha256=preserved_sha,
                expected_size=preserved_size,
                label="preservation copy",
            )
        _attest_single_link_evidence(
            manifest_path,
            expected_identity=manifest_identity,
            expected_sha256=manifest_sha,
            label="preservation manifest",
        )

        result_evidence = {
            "plan_fingerprint": rebuilt.evidence_fingerprint,
            "stage_state": stage_state,
            "preserved_sha256": preserved_sha,
            "preserved_size_bytes": preserved_size,
            "manifest_sha256": manifest_sha,
            "target_final": _observation_payload(target_final),
            "donor_final": _observation_payload(donor_final),
            "target_replacement_performed": False,
            "donor_materialized": False,
        }
        result_fingerprint = _fingerprint(result_evidence)
        conn.execute(
            """
            INSERT INTO archive_recovery_preservation_stages(
                stage_id,proposal_id,target_file_id,target_photo_id,library_id,donor_file_id,
                expected_revision_id,expected_sha256,recovery_intent_resolution_id,
                phase13_evidence_fingerprint,phase14_plan_fingerprint,target_path,target_state,
                target_observed_sha256,target_size_bytes,target_mtime_ns,target_fs_device_id,target_fs_object_id,
                donor_path,donor_observed_sha256,donor_size_bytes,donor_mtime_ns,donor_fs_device_id,donor_fs_object_id,
                preservation_root,preservation_path,preserved_sha256,preserved_size_bytes,
                manifest_path,manifest_sha256,stage_state,target_replacement_performed,donor_materialized,
                recovery_execution_authorized,evidence_fingerprint,note,staged_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rebuilt.stage_id,rebuilt.proposal_id,rebuilt.file_id,rebuilt.photo_id,rebuilt.library_id,
                rebuilt.donor_file_id,rebuilt.expected_revision_id,rebuilt.expected_sha256,
                rebuilt.recovery_intent_resolution_id,rebuilt.phase13_evidence_fingerprint,
                rebuilt.evidence_fingerprint,rebuilt.target_path,rebuilt.target_state,
                rebuilt.target_observed_sha256,rebuilt.target_size_bytes,rebuilt.target_mtime_ns,
                rebuilt.target_fs_device_id,rebuilt.target_fs_object_id,rebuilt.donor_path,
                rebuilt.donor_observed_sha256,rebuilt.donor_size_bytes,rebuilt.donor_mtime_ns,
                rebuilt.donor_fs_device_id,rebuilt.donor_fs_object_id,str(root),
                None if preservation_path is None else str(preservation_path),preserved_sha,preserved_size,
                str(manifest_path),manifest_sha,stage_state,0,0,0,result_fingerprint,note,staged_at,
            ),
        )
        conn.execute(
            "INSERT INTO integrity_events(file_id,event_type,detail) VALUES (?,?,?)",
            (
                rebuilt.file_id,
                "archive_recovery_preservation_staged",
                (
                    f"Recovery preservation stage {rebuilt.stage_id} recorded for proposal {rebuilt.proposal_id}; "
                    + (f"suspect bytes preserved as SHA-256 {preserved_sha}. " if preserved_sha else "target was missing; no suspect bytes existed to preserve. ")
                    + "Donor bytes were not materialized and the source target was not replaced."
                ),
            ),
        )
        conn.commit()
        checkpoint_committed = True

        # Phase 14.1.17.4: the immutable catalogue checkpoint is the authority.
        # Do not mutate evidence metadata after commit; advisory chmod created a
        # hard-link side-effect boundary without adding real authority.
        return RecoveryPreservationResult(
            schema=PRESERVATION_RESULT_SCHEMA,
            stage_id=rebuilt.stage_id,
            proposal_id=rebuilt.proposal_id,
            stage_state=stage_state,
            preservation_path=None if preservation_path is None else str(preservation_path),
            preserved_sha256=preserved_sha,
            preserved_size_bytes=preserved_size,
            manifest_path=str(manifest_path),
            manifest_sha256=manifest_sha,
            target_replacement_performed=False,
            donor_materialized=False,
            staged_at=staged_at,
            evidence_fingerprint=result_fingerprint,
        )
    except BaseException:
        # Filesystem cleanup is valid only while the catalogue checkpoint is
        # still rollback-able.  An interruption can happen after SQLite commits
        # but before returning to the caller; deleting evidence at that point
        # would leave an immutable DB row pointing at missing files.
        durable = checkpoint_committed or _preservation_checkpoint_is_durable(
            conn, checkpoint_stage_id
        )
        if not durable:
            try:
                if conn.in_transaction:
                    conn.rollback()
            finally:
                _safe_cleanup_created_stage(
                    bound_stage,
                    owned_paths=owned_stage_paths,
                )
        raise
    finally:
        # ``bound_stage`` is the POSIX stage_authority object; close it only once.
        if stage_authority is not None:
            try:
                stage_authority.close()
            except Exception:
                pass
        if root_authority is not None:
            try:
                root_authority.close()
            except Exception:
                pass


def list_preservation_stages(conn: Connection, *, target_file_id: str | None = None):
    if target_file_id is None:
        return conn.execute(
            "SELECT * FROM archive_recovery_preservation_stages ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM archive_recovery_preservation_stages WHERE target_file_id=? ORDER BY id",
        (target_file_id,),
    ).fetchall()


def concise_preservation_plan_text(plan: RecoveryPreservationPlan) -> str:
    lines = [
        "Phase 14.0 — Recovery Preservation Staging",
        f"Stage: {plan.stage_id}",
        f"Frozen proposal: {plan.proposal_id}",
        f"Target: {plan.file_id}  {plan.target_path}",
        f"Donor:  {plan.donor_file_id}  {plan.donor_path}",
        f"Target state: {plan.target_state}",
        f"Preservation required: {'YES' if plan.preservation_required else 'NO — target missing'}",
        f"Preservation path: {plan.preservation_path or 'not applicable'}",
        f"Operational free bytes: {plan.available_bytes}",
        "Target replacement authorised: NO",
        "Donor materialisation authorised: NO",
        "Recovery execution authorised: NO",
        f"Evidence fingerprint: {plan.evidence_fingerprint}",
    ]
    return "\n".join(lines)


def concise_preservation_result_text(result: RecoveryPreservationResult) -> str:
    lines = [
        "Phase 14.0 — Preservation staging complete",
        f"Stage: {result.stage_id}",
        f"State: {result.stage_state}",
        f"Preserved path: {result.preservation_path or 'not applicable'}",
        f"Preserved SHA-256: {result.preserved_sha256 or 'not applicable'}",
        f"Manifest: {result.manifest_path}",
        "Target replacement performed: NO",
        "Donor materialised: NO",
    ]
    return "\n".join(lines)
