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
import tempfile
import uuid

from ppa.hashing import sha256_file
from ppa.physical_observation import PhysicalObservationError, StableFileObservation, observe_stable_image
from ppa.recovery_planning import RecoveryPlanningError, build_recovery_plan

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
    if absolute.exists() and absolute.is_symlink():
        raise RecoveryPreservationError("recovery preservation root may not be a symbolic link")
    if absolute.exists() and not absolute.is_dir():
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
    try:
        st = path.lstat()
    except OSError as exc:
        raise RecoveryPreservationError("preservation stage directory disappeared") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RecoveryPreservationError("preservation stage directory identity is unsafe")
    return int(st.st_dev), int(st.st_ino)


def _regular_file_identity(path: Path) -> tuple[int, int]:
    try:
        st = path.lstat()
    except OSError as exc:
        raise RecoveryPreservationError("preservation evidence file disappeared") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise RecoveryPreservationError("preservation evidence file identity is unsafe")
    return int(st.st_dev), int(st.st_ino)


def _chmod_mode_if_same(path: Path, expected_identity: tuple[int, int], mode: int) -> bool:
    """Change mode only through a descriptor proven to be the expected object."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        if (int(st.st_dev), int(st.st_ino)) != expected_identity:
            return False
        try:
            os.fchmod(fd, mode)
        except (OSError, AttributeError):
            return False
        return True
    finally:
        os.close(fd)


def _safe_cleanup_created_stage(
    stage_dir: Path | None,
    *,
    root: Path | None,
    expected_identity: tuple[int, int] | None,
    owned_paths: set[Path],
) -> None:
    """Best-effort rollback cleanup without recursive traversal.

    Cleanup is deliberately narrower than ``shutil.rmtree``.  It only operates
    when the stage directory is still the exact directory object PPA created,
    and only unlinks known PPA-owned artifacts.  Unexpected children are left
    in place for later diagnosis rather than chmodded/traversed recursively.
    """
    if stage_dir is None or root is None or expected_identity is None:
        return
    try:
        if os.path.dirname(_canonical(stage_dir)) != _canonical(root):
            return
        st = stage_dir.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return
        if (int(st.st_dev), int(st.st_ino)) != expected_identity:
            return
    except OSError:
        return

    for owned in tuple(owned_paths):
        try:
            # Never follow an alias during cleanup.  The path itself must still
            # be a direct child of the identity-bound stage directory.
            if owned.parent != stage_dir:
                continue
            lst = owned.lstat()
            if stat.S_ISDIR(lst.st_mode) and not stat.S_ISLNK(lst.st_mode):
                # Phase 14.0 creates no child directories.
                continue
            try:
                owned.unlink()
            except PermissionError:
                if not stat.S_ISLNK(lst.st_mode) and stat.S_ISREG(lst.st_mode):
                    _chmod_mode_if_same(owned, (int(lst.st_dev), int(lst.st_ino)), 0o600)
                owned.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    try:
        # rmdir succeeds only when no unexpected content is present.
        stage_dir.rmdir()
    except OSError:
        pass


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


def _copy_preserved_bytes(source: Path, temporary: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as src, temporary.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            dst.write(chunk)
            total += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
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


def _write_manifest(path: Path, payload: dict) -> str:
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="manifest.", suffix=".tmp", dir=str(path.parent))
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


def _chmod_read_only(path: Path, expected_identity: tuple[int, int]) -> None:
    # Descriptor-bound chmod avoids following a path that was replaced with a
    # symlink/hardlink alias after the preservation transaction committed.
    _chmod_mode_if_same(path, expected_identity, 0o444)


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
    owned_stage_paths: set[Path] = set()
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

        root = _validate_preservation_root(conn, Path(plan.preservation_root))
        root.mkdir(parents=True, exist_ok=True)
        root = _validate_preservation_root(conn, root)
        stage_root = root
        # Revalidate even caller-supplied plans before using the identifier as a
        # path component.  A plan object is evidence, not filesystem authority.
        stage_id = _validated_stage_id(plan.stage_id)
        if stage_id != rebuilt.stage_id:
            raise RecoveryPreservationError("preservation stage ID changed while rebuilding the plan")
        stage_dir = root / stage_id
        if stage_dir.exists() or stage_dir.is_symlink():
            raise RecoveryPreservationError("preservation stage destination already exists")
        stage_dir.mkdir(mode=0o700)
        stage_dir_identity = _directory_identity(stage_dir)
        # Persist the parent directory entry before any preservation evidence is
        # allowed to become a committed catalogue checkpoint.
        _fsync_directory(root)
        # Revalidate the root after creating the child so a parent-path change
        # cannot silently redirect the operational store into a source Library.
        root = _validate_preservation_root(conn, root)
        if stage_dir.is_symlink() or os.path.dirname(_canonical(stage_dir)) != _canonical(root):
            raise RecoveryPreservationError("preservation stage directory identity is unsafe")
        if _directory_identity(stage_dir) != stage_dir_identity:
            raise RecoveryPreservationError("preservation stage directory changed after creation")

        # Re-check free space after creating the operational directory and before
        # copying any source-derived bytes.
        free_now = _free_bytes_for(stage_dir)
        if rebuilt.preservation_required and free_now < rebuilt.required_bytes + _MIN_FREE_RESERVE_BYTES:
            raise RecoveryPreservationError("insufficient operational storage at preservation execution time")

        preserved_sha: str | None = None
        preserved_size: int | None = None
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
            fd, temp_name = tempfile.mkstemp(
                prefix="suspect-source.", suffix=".pending", dir=str(stage_dir)
            )
            os.close(fd)
            temp_path = Path(temp_name)
            owned_stage_paths.add(temp_path)
            try:
                copied_sha, copied_size = _copy_preserved_bytes(Path(rebuilt.target_path), temp_path)
                if copied_sha != rebuilt.target_observed_sha256 or copied_size != rebuilt.target_size_bytes:
                    raise RecoveryPreservationError(
                        "target changed while suspect bytes were being preserved; staged bytes discarded"
                    )
                # Independent readback proves the actual preservation file, not
                # merely the streaming digest calculated while writing it.
                readback_sha = sha256_file(temp_path)
                if readback_sha != copied_sha or temp_path.stat().st_size != copied_size:
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

                os.replace(temp_path, preservation_path)
                owned_stage_paths.discard(temp_path)
                owned_stage_paths.add(preservation_path)
                _fsync_directory(stage_dir)
                preserved_sha = readback_sha
                preserved_size = copied_size
            finally:
                temp_path.unlink(missing_ok=True)
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
        manifest_sha = _write_manifest(manifest_path, manifest_payload)
        owned_stage_paths.add(manifest_path)

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

        # Prove the operational evidence itself one final time immediately before
        # the catalogue checkpoint.  A staged file/manifest that changed after
        # initial write verification is not accepted as preservation evidence.
        preservation_identity: tuple[int, int] | None = None
        if preservation_path is not None:
            try:
                final_preserved_sha = sha256_file(preservation_path)
                final_preserved_size = int(preservation_path.stat().st_size)
            except OSError as exc:
                raise RecoveryPreservationError("preservation copy disappeared before commit") from exc
            if final_preserved_sha != preserved_sha or final_preserved_size != preserved_size:
                raise RecoveryPreservationError("preservation copy changed before commit; checkpoint rolled back")
            preservation_identity = _regular_file_identity(preservation_path)
        try:
            final_manifest_sha = sha256_file(manifest_path)
        except OSError as exc:
            raise RecoveryPreservationError("preservation manifest disappeared before commit") from exc
        if final_manifest_sha != manifest_sha:
            raise RecoveryPreservationError("preservation manifest changed before commit; checkpoint rolled back")
        manifest_identity = _regular_file_identity(manifest_path)

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

        if preservation_path is not None and preservation_identity is not None:
            _chmod_read_only(preservation_path, preservation_identity)
        _chmod_read_only(manifest_path, manifest_identity)
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
    except Exception:
        try:
            conn.rollback()
        finally:
            _safe_cleanup_created_stage(
                stage_dir,
                root=stage_root,
                expected_identity=stage_dir_identity,
                owned_paths=owned_stage_paths,
            )
        raise


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
