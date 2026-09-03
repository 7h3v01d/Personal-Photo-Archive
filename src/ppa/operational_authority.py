"""Identity-bearing positive ownership for PPA operational objects (14.1.15)."""
from __future__ import annotations
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from ppa.secure_write import is_windows_reparse_point_stat

class OperationalAuthorityError(ValueError):
    """An exact filesystem object lacks positive PPA operational ownership."""

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))

def _identity(authority) -> tuple[str, str]:
    return (str(authority.identity[0]), str(authority.identity[1]))

def _persist(conn: Connection, sql: str, params: tuple) -> None:
    outer = conn.in_transaction
    conn.execute(sql, params)
    if not outer:
        conn.commit()

def directory_records(conn: Connection, purpose: str):
    return conn.execute(
        "SELECT canonical_path,fs_device_id,fs_object_id FROM operational_directories WHERE purpose=? ORDER BY id",
        (purpose,),
    ).fetchall()

def _enroll_directory(
    conn: Connection,
    purpose: str,
    authority,
    *,
    allow_multiple: bool = False,
) -> None:
    """Persist the exact already-bound directory object as operational authority."""
    authority.verify_pathname()
    ident = _identity(authority)
    path = canonical(authority.path)
    rows = directory_records(conn, purpose)
    matching_path = next((r for r in rows if canonical(r[0]) == path), None)
    if matching_path is not None:
        if (str(matching_path[1]), str(matching_path[2])) != ident:
            raise OperationalAuthorityError(
                f"{purpose} pathname does not name the enrolled PPA operational object"
            )
        _persist(
            conn,
            "UPDATE operational_directories SET verified_at=? WHERE purpose=? AND canonical_path=?",
            (_now(), purpose, matching_path[0]),
        )
        authority.verify_pathname()
        return
    if rows and not allow_multiple:
        raise OperationalAuthorityError(
            f"{purpose} operational object moved or was replaced; explicit relocation is required"
        )
    now = _now()
    try:
        _persist(
            conn,
            "INSERT INTO operational_directories(purpose,canonical_path,fs_device_id,fs_object_id,created_at,verified_at) VALUES (?,?,?,?,?,?)",
            (purpose, path, *ident, now, now),
        )
    except Exception as exc:
        raise OperationalAuthorityError(f"could not enroll {purpose} operational directory") from exc
    authority.verify_pathname()


def require_directory(
    conn: Connection,
    purpose: str,
    authority,
    *,
    allow_multiple: bool = False,
) -> None:
    """Require exact enrolled authority or creator-issued creation provenance.

    An unenrolled directory may be enrolled implicitly only when the *bound
    authority itself* proves that its final component was created by the exact
    exclusive creation primitive that returned it.  Caller observations such as
    ``path.exists() == False`` are intentionally incapable of conferring
    operational ownership.
    """
    authority.verify_pathname()
    ident = _identity(authority)
    path = canonical(authority.path)
    rows = directory_records(conn, purpose)
    matching_path = next((r for r in rows if canonical(r[0]) == path), None)
    if matching_path is not None:
        if (str(matching_path[1]), str(matching_path[2])) != ident:
            raise OperationalAuthorityError(
                f"{purpose} pathname does not name the enrolled PPA operational object"
            )
        _persist(
            conn,
            "UPDATE operational_directories SET verified_at=? WHERE purpose=? AND canonical_path=?",
            (_now(), purpose, matching_path[0]),
        )
        authority.verify_pathname()
        return
    if rows and not allow_multiple:
        raise OperationalAuthorityError(
            f"{purpose} operational object moved or was replaced; explicit relocation is required"
        )
    if not bool(getattr(authority, "final_component_created_by_this_operation", False)):
        raise OperationalAuthorityError(
            f"{purpose} directory is not an enrolled PPA operational object and was not created by this secure operation"
        )
    _enroll_directory(conn, purpose, authority, allow_multiple=allow_multiple)


def enroll_existing_directory(
    conn: Connection,
    purpose: str,
    authority,
    *,
    allow_multiple: bool = False,
) -> None:
    """Explicit trusted enrollment after independent validation of exact authority.

    This is the intentional manual trust boundary.  It does not manufacture
    creator provenance and is kept separate from ``require_directory``.
    """
    _enroll_directory(conn, purpose, authority, allow_multiple=allow_multiple)


def _safe_regular_identity(path: str | Path) -> tuple[int, int] | None:
    path = Path(path)
    try:
        st = path.lstat()
    except OSError:
        return None
    if (
        stat.S_ISLNK(st.st_mode)
        or is_windows_reparse_point_stat(st)
        or not stat.S_ISREG(st.st_mode)
    ):
        return None
    return int(st.st_dev), int(st.st_ino)


def owned_file_identity(conn: Connection, purpose: str, path: str | Path) -> tuple[int, int] | None:
    """Return the persisted identity only when the pathname still names it now."""
    cp = canonical(path)
    row = conn.execute(
        "SELECT fs_device_id,fs_object_id FROM operational_files WHERE purpose=? AND canonical_path=?",
        (purpose, cp),
    ).fetchone()
    if row is None:
        return None
    expected = (int(row[0]), int(row[1]))
    actual = _safe_regular_identity(path)
    if actual != expected:
        return None
    _persist(
        conn,
        "UPDATE operational_files SET verified_at=? WHERE purpose=? AND canonical_path=?",
        (_now(), purpose, cp),
    )
    return expected


def is_owned_file(conn: Connection, purpose: str, path: str | Path) -> bool:
    return owned_file_identity(conn, purpose, path) is not None


def record_owned_file_identity(
    conn: Connection,
    purpose: str,
    path: str | Path,
    expected_identity: tuple[int, int],
    *,
    canonical_path: str | None = None,
) -> None:
    """Persist ownership from the installer-held identity, never a later path stat.

    ``expected_identity`` must come from the descriptor/handle of the object PPA
    actually created and installed.  The pathname may already have been swapped
    by the time this function runs; that cannot cause the replacement object to
    be blessed because no filesystem identity is re-learned here.
    """
    cp = canonical_path if canonical_path is not None else canonical(path)
    ident = (str(int(expected_identity[0])), str(int(expected_identity[1])))
    now = _now()
    row = conn.execute(
        "SELECT id FROM operational_files WHERE purpose=? AND canonical_path=?",
        (purpose, cp),
    ).fetchone()
    if row is None:
        _persist(
            conn,
            "INSERT INTO operational_files(purpose,canonical_path,fs_device_id,fs_object_id,created_at,verified_at) VALUES (?,?,?,?,?,?)",
            (purpose, cp, *ident, now, now),
        )
    else:
        _persist(
            conn,
            "UPDATE operational_files SET fs_device_id=?,fs_object_id=?,verified_at=? WHERE id=?",
            (*ident, now, int(row[0])),
        )


def record_owned_file(conn: Connection, purpose: str, path: str | Path) -> None:
    """Compatibility wrapper for explicitly trusted existing objects.

    Security-sensitive creation/install paths should call
    ``record_owned_file_identity`` with the identity they already hold.
    """
    ident = _safe_regular_identity(path)
    if ident is None:
        raise OperationalAuthorityError("operational file is not a safe regular filesystem object")
    record_owned_file_identity(conn, purpose, path, ident)
