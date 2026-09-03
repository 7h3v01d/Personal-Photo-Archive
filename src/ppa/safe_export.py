"""Archive-safe user-directed output helpers.

User-selected export paths are never allowed to target a registered source
Library or PPA's operational state.  Writes are atomic via a sibling temporary
file so an existing symlink/hard-link output alias is never opened for writing.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection

from ppa.secure_write import (
    BoundTemporaryFile, SecureWriteError, bind_directory_authority,
    ensure_directory_authority,
)
from ppa.source_tree_authority import SourceTreeAuthorityError, SourceTreeAuthorityPolicy
from ppa.operational_authority import (
    OperationalAuthorityError, enroll_existing_directory, owned_file_identity,
    record_owned_file_identity, require_directory,
)
from typing import BinaryIO, Iterator


class ArchiveOutputSafetyError(ValueError):
    """Raised when a requested export destination crosses an archive boundary."""


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


def _db_path_from_conn(conn: Connection | None) -> Path | None:
    if conn is None:
        return None
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            # row: seq, name, file
            if row[1] == "main" and row[2]:
                return Path(row[2])
    except Exception:
        return None
    return None


def _borrow_conn(conn: Connection | None, config):
    if conn is not None:
        return conn, False
    db_path = getattr(config, "db_path", None) if config is not None else None
    if db_path is None or not Path(db_path).exists():
        return None, False
    try:
        opened = sqlite3.connect(Path(db_path))
        opened.row_factory = sqlite3.Row
        return opened, True
    except sqlite3.Error:
        return None, False


def _library_roots(conn: Connection | None, config) -> tuple[str, ...]:
    roots: set[str] = set()
    borrowed, must_close = _borrow_conn(conn, config)
    try:
        if borrowed is not None:
            try:
                columns = {r[1] for r in borrowed.execute("PRAGMA table_info(libraries)").fetchall()}
                column = (
                    "root_canonical_path" if "root_canonical_path" in columns
                    else "canonical_path" if "canonical_path" in columns
                    else None
                )
                if column is not None:
                    rows = borrowed.execute(f"SELECT {column} FROM libraries ORDER BY id").fetchall()
                    roots.update(_canonical(Path(r[0])) for r in rows if r[0])
            except sqlite3.Error:
                pass
    finally:
        if must_close and borrowed is not None:
            borrowed.close()
    if config is not None:
        for path in getattr(config, "library_directories", ()) or ():
            roots.add(_canonical(Path(path)))
    return tuple(sorted(roots))


def _source_tree_policy(conn: Connection | None, config) -> SourceTreeAuthorityPolicy:
    """Return one immutable source-tree authority snapshot for this export."""
    borrowed, must_close = _borrow_conn(conn, config)
    try:
        if borrowed is not None:
            try:
                return SourceTreeAuthorityPolicy.from_connection(borrowed)
            except SourceTreeAuthorityError as exc:
                raise ArchiveOutputSafetyError(str(exc)) from exc
    finally:
        if must_close and borrowed is not None:
            borrowed.close()

    configured = tuple(getattr(config, "library_directories", ()) or ()) if config is not None else ()
    if configured:
        # Root-only best effort would recreate the moved-child bypass.  Without
        # catalogue history there is no sound way to classify the full source tree.
        raise ArchiveOutputSafetyError(
            "Archive-safe output requires catalogue-backed source-tree filesystem identity; "
            "open/rescan the registered Libraries before exporting."
        )
    return SourceTreeAuthorityPolicy.empty()


def _validate_bound_export_parent(
    authority, destination: Path, *, conn, config, source_policy: SourceTreeAuthorityPolicy
) -> None:
    """Prove the already-bound parent object is not source-tree authority."""
    try:
        source_policy.validate_authority(authority, purpose="export parent")
    except SourceTreeAuthorityError as exc:
        raise ArchiveOutputSafetyError(str(exc)) from exc
    # Path/topology policy remains useful, but is evaluated after exact object
    # selection and while that object remains pinned.
    validate_export_destination(destination, conn=conn, config=config)
    authority.verify_pathname()


def _operational_paths(conn: Connection | None, config) -> tuple[tuple[str, bool], ...]:
    """Return (canonical path, is_tree) protected operational destinations."""
    protected: list[tuple[str, bool]] = []
    db_path = _db_path_from_conn(conn)
    if db_path is None and config is not None and getattr(config, "db_path", None) is not None:
        db_path = Path(config.db_path)
    if db_path is not None:
        db = Path(db_path)
        protected.extend([
            (_canonical(db), False),
            (_canonical(Path(str(db) + "-wal")), False),
            (_canonical(Path(str(db) + "-shm")), False),
            (_canonical(db.parent / "thumbnails"), True),
            (_canonical(db.parent / "recovery-preservation"), True),
        ])
    if config is not None and getattr(config, "log_path", None) is not None:
        log = Path(config.log_path)
        structured = log.with_name(log.stem + ".jsonl")
        protected.extend([
            (_canonical(log), False),
            (_canonical(structured), False),
        ])

    # Phase 14 permits an internal API caller to choose an alternate operational
    # preservation root.  Once a successful preservation stage records such a
    # root, ordinary exports must protect that tree exactly like the default
    # db-adjacent recovery-preservation directory.
    borrowed, must_close = _borrow_conn(conn, config)
    try:
        if borrowed is not None:
            try:
                table = borrowed.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='archive_recovery_preservation_stages'"
                ).fetchone()
                if table is not None:
                    for row in borrowed.execute(
                        "SELECT DISTINCT preservation_root "
                        "FROM archive_recovery_preservation_stages "
                        "WHERE preservation_root IS NOT NULL"
                    ):
                        if row[0]:
                            protected.append((_canonical(Path(row[0])), True))
            except sqlite3.Error:
                pass
    finally:
        if must_close and borrowed is not None:
            borrowed.close()
    return tuple(protected)


def validate_export_destination(
    destination: str | Path,
    *,
    conn: Connection | None = None,
    config=None,
) -> Path:
    """Validate and return an absolute output path.

    Fail closed when the resolved destination is inside any registered source
    Library, aliases a catalogued source File, or collides with protected PPA
    operational state.  The destination may be outside those trees even if it
    already exists; actual writers use atomic replacement rather than opening
    the existing destination inode for writing.
    """
    raw = Path(destination).expanduser()
    absolute = raw if raw.is_absolute() else Path.cwd() / raw
    canonical = _canonical(absolute)

    for root in _library_roots(conn, config):
        if canonical == root or _within(canonical, root):
            raise ArchiveOutputSafetyError(
                "Export destination is inside a registered source Library; "
                "choose a location outside the archive tree."
            )

    for protected, is_tree in _operational_paths(conn, config):
        if canonical == protected or (is_tree and _within(canonical, protected)):
            raise ArchiveOutputSafetyError(
                "Export destination collides with protected PPA operational state."
            )

    borrowed, must_close = _borrow_conn(conn, config)
    try:
        if borrowed is not None:
            # Canonical-path equality catches leaf/parent symlink aliases.
            try:
                for row in borrowed.execute("SELECT path FROM files WHERE path IS NOT NULL"):
                    if row[0] and _canonical(Path(row[0])) == canonical:
                        raise ArchiveOutputSafetyError(
                            "Export destination resolves to a catalogued source File."
                        )
            except sqlite3.Error:
                pass

            # Existing hard-link aliases outside a Library are rejected too.
            if absolute.exists():
                try:
                    st = absolute.stat()
                    hit = borrowed.execute(
                        "SELECT id FROM files WHERE fs_device_id=? AND fs_object_id=? LIMIT 1",
                        (str(getattr(st, "st_dev", "")), str(getattr(st, "st_ino", ""))),
                    ).fetchone()
                except (OSError, sqlite3.Error):
                    hit = None
                if hit is not None:
                    raise ArchiveOutputSafetyError(
                        "Export destination is the same filesystem object as a catalogued source File."
                    )
    finally:
        if must_close and borrowed is not None:
            borrowed.close()

    return absolute.resolve(strict=False)


def enroll_export_root(path: str | Path, *, conn: Connection, config=None) -> Path:
    """Explicitly enroll an existing user-selected export directory object.

    Enrollment is a trust decision, never an implicit side effect of export.
    The exact object is first bound and proven outside all source/operational
    trees, then its identity is persisted for future exports.
    """
    root = Path(path).expanduser()
    absolute = root if root.is_absolute() else Path.cwd() / root
    if not absolute.exists() or not absolute.is_dir():
        raise ArchiveOutputSafetyError("export root enrollment requires an existing directory")
    policy = _source_tree_policy(conn, config)
    authority = None
    try:
        authority = bind_directory_authority(absolute)
        try:
            policy.validate_authority(authority, purpose="export root")
        except SourceTreeAuthorityError as exc:
            raise ArchiveOutputSafetyError(str(exc)) from exc
        # Validate a synthetic child so operational/source path policy is reused.
        validate_export_destination(absolute / ".ppa-export-enrollment-probe", conn=conn, config=config)
        try:
            enroll_existing_directory(conn, "export_root", authority, allow_multiple=True)
        except OperationalAuthorityError as exc:
            raise ArchiveOutputSafetyError(str(exc)) from exc
        return absolute.resolve(strict=False)
    except SecureWriteError as exc:
        raise ArchiveOutputSafetyError(str(exc)) from exc
    finally:
        if authority is not None:
            authority.close()


@contextmanager
def safe_export_temp(
    destination: str | Path,
    *,
    conn: Connection | None = None,
    config=None,
) -> Iterator[tuple[Path, BinaryIO]]:
    """Yield a descriptor-bound export temporary under positive ownership.

    The export parent must either be an already-enrolled exact export-root
    object or be newly created by this operation. Existing destination files
    are replaceable only when their exact filesystem object is already recorded
    as a PPA-created export.
    """
    borrowed, must_close = _borrow_conn(conn, config)
    if borrowed is None:
        configured = tuple(getattr(config, "library_directories", ()) or ()) if config is not None else ()
        if configured:
            raise ArchiveOutputSafetyError(
                "Archive-safe export requires a file-backed catalogue when source Libraries are configured"
            )
        # Utility/report mode with no catalogue and no registered source context:
        # retain descriptor-bound atomic output, but there is no durable PPA
        # authority database in which ownership could be enrolled.
        out = validate_export_destination(destination, conn=None, config=config)
        parent_authority = None
        temp = None
        try:
            parent_authority = ensure_directory_authority(out.parent)
            expected = tuple(parent_authority.identity)
            temp = BoundTemporaryFile.create(out.parent, prefix=out.name + ".", suffix=".tmp", expected_parent_identity=expected)
            parent_authority.close(); parent_authority = None
            with temp.binary_writer() as writer:
                yield out, writer
            temp.sync_and_verify()
            validate_export_destination(out, conn=None, config=config)
            temp.install(out, replace=True)
            return
        except SecureWriteError as exc:
            raise ArchiveOutputSafetyError(str(exc)) from exc
        finally:
            if temp is not None:
                temp.cleanup()
            if parent_authority is not None:
                parent_authority.close()
    effective_conn = borrowed
    parent_authority = None
    temp: BoundTemporaryFile | None = None
    try:
        out = validate_export_destination(destination, conn=effective_conn, config=config)
        try:
            source_policy = SourceTreeAuthorityPolicy.from_connection(effective_conn)
        except SourceTreeAuthorityError as exc:
            raise ArchiveOutputSafetyError(str(exc)) from exc
        parent_authority = ensure_directory_authority(
            out.parent,
            validator=lambda authority: _validate_bound_export_parent(
                authority, out, conn=effective_conn, config=config, source_policy=source_policy
            ),
        )
        try:
            require_directory(
                effective_conn, "export_root", parent_authority, allow_multiple=True,
            )
        except OperationalAuthorityError as exc:
            raise ArchiveOutputSafetyError(str(exc)) from exc

        destination_existed = os.path.lexists(os.fspath(out))
        if destination_existed and owned_file_identity(effective_conn, "export", out) is None:
            raise ArchiveOutputSafetyError(
                "existing export destination is not a positively owned PPA export; refusing replacement"
            )

        # Capture the intended record key before installation.  Ownership will be
        # persisted from temp.identity, never re-learned by statting this pathname.
        owned_canonical_path = _canonical(out)
        approved_parent_identity = tuple(parent_authority.identity)
        parent_authority.verify_pathname()
        temp = BoundTemporaryFile.create(
            out.parent, prefix=out.name + ".", suffix=".tmp",
            expected_parent_identity=approved_parent_identity,
        )
        parent_authority.close(); parent_authority = None
        with temp.binary_writer() as writer:
            yield out, writer
        temp.sync_and_verify()
        validate_export_destination(out, conn=effective_conn, config=config)
        # Derive a specific expected identity immediately before installation.
        # The installer itself re-verifies that exact object before parking it,
        # closing the ownership-check -> replace TOCTOU window.
        if os.path.lexists(os.fspath(out)):
            expected_existing_identity = owned_file_identity(effective_conn, "export", out)
            if expected_existing_identity is None:
                raise ArchiveOutputSafetyError(
                    "export destination object appeared or changed and is not PPA-owned"
                )
            replace = True
        else:
            expected_existing_identity = None
            replace = False
        temp.install(
            out, replace=replace,
            expected_existing_identity=expected_existing_identity,
        )
        record_owned_file_identity(
            effective_conn, "export", out, tuple(temp.identity),
            canonical_path=owned_canonical_path,
        )
    except SecureWriteError as exc:
        raise ArchiveOutputSafetyError(str(exc)) from exc
    finally:
        if temp is not None:
            temp.cleanup()
        if parent_authority is not None:
            parent_authority.close()
        if must_close:
            effective_conn.close()


def safe_export_text(
    destination: str | Path,
    contents: str,
    *,
    conn: Connection | None = None,
    config=None,
    encoding: str = "utf-8",
) -> Path:
    with safe_export_temp(destination, conn=conn, config=config) as (out, writer):
        writer.write(contents.encode(encoding))
    return out


def safe_export_bytes(
    destination: str | Path,
    contents: bytes,
    *,
    conn: Connection | None = None,
    config=None,
) -> Path:
    with safe_export_temp(destination, conn=conn, config=config) as (out, writer):
        writer.write(contents)
    return out
