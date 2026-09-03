"""Shared source-tree filesystem-object authority policy (Phase 14.1.13).

A registered Library is a *tree of source directory objects*, not merely a root
pathname.  Once a complete scan has observed a directory object under a Library,
that object's filesystem identity remains source-associated until the Library is
explicitly forgotten.  Writable operational stores must reject any bound object
whose identity appears in this historical source-tree set, even if the directory
has since been renamed outside the Library pathname.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection


class SourceTreeAuthorityError(ValueError):
    """Source-tree authority cannot be established safely."""


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


@dataclass(frozen=True)
class SourceTreeAuthorityPolicy:
    """Immutable snapshot of known source-tree directory authority."""

    forbidden_directory_identities: frozenset[tuple[str, str]]
    forbidden_roots: tuple[str, ...]

    @classmethod
    def empty(cls) -> "SourceTreeAuthorityPolicy":
        return cls(frozenset(), ())

    @classmethod
    def from_connection(cls, conn: Connection) -> "SourceTreeAuthorityPolicy":
        try:
            library_columns = {
                r[1] for r in conn.execute("PRAGMA table_info(libraries)").fetchall()
            }
            required = {
                "root_canonical_path",
                "root_fs_device_id",
                "root_fs_object_id",
                "source_tree_identity_complete",
                "source_tree_identity_verified_at",
            }
            if not required.issubset(library_columns):
                raise SourceTreeAuthorityError(
                    "catalogue lacks complete source-tree filesystem authority; "
                    "rescan Libraries with the current PPA version before creating writable operational output"
                )

            libraries = conn.execute(
                "SELECT id,root_canonical_path,root_fs_device_id,root_fs_object_id,"
                "source_tree_identity_complete,source_tree_identity_verified_at "
                "FROM libraries ORDER BY id"
            ).fetchall()
            if not libraries:
                return cls.empty()

            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='library_directory_identities'"
            ).fetchone()
            if table is None:
                raise SourceTreeAuthorityError(
                    "catalogue lacks source-tree directory identity evidence; rescan Libraries"
                )

            roots: list[str] = []
            root_by_library: dict[int, tuple[str, str]] = {}
            for row in libraries:
                library_id = int(row[0])
                root_path, dev, obj, complete, verified_at = row[1:]
                if dev is None or obj is None:
                    raise SourceTreeAuthorityError(
                        "registered Library root filesystem identity is not yet verified; rescan the Library"
                    )
                if int(complete or 0) != 1 or not verified_at:
                    raise SourceTreeAuthorityError(
                        "registered Library source-tree filesystem identity is not completely verified; "
                        "run a complete Library scan before creating writable operational output"
                    )
                roots.append(_canonical(Path(root_path)))
                root_by_library[library_id] = (str(dev), str(obj))

            rows = conn.execute(
                "SELECT library_id,fs_device_id,fs_object_id "
                "FROM library_directory_identities ORDER BY library_id,id"
            ).fetchall()
            identities = frozenset((str(r[1]), str(r[2])) for r in rows)

            # A complete source-tree claim must include the root object itself.
            observed_by_library: dict[int, set[tuple[str, str]]] = {}
            for row in rows:
                observed_by_library.setdefault(int(row[0]), set()).add(
                    (str(row[1]), str(row[2]))
                )
            for library_id, root_identity in root_by_library.items():
                if root_identity not in observed_by_library.get(library_id, set()):
                    raise SourceTreeAuthorityError(
                        "registered Library source-tree identity inventory is incomplete; rescan the Library"
                    )
            return cls(identities, tuple(sorted(set(roots))))
        except sqlite3.Error as exc:
            raise SourceTreeAuthorityError(
                "could not load registered Library source-tree filesystem authority"
            ) from exc

    def validate_authority(self, authority, *, purpose: str = "operational directory") -> None:
        """Reject an exact bound directory object that belongs to source data."""
        authority.verify_pathname()
        identity = (str(authority.identity[0]), str(authority.identity[1]))
        if identity in self.forbidden_directory_identities:
            raise SourceTreeAuthorityError(
                f"{purpose} is a historically observed directory object from a registered source Library tree"
            )
        candidate = _canonical(Path(authority.path))
        for root in self.forbidden_roots:
            if candidate == root or _within(candidate, root):
                raise SourceTreeAuthorityError(
                    f"{purpose} resolves inside a registered source Library tree"
                )
        authority.verify_pathname()


def record_library_directory_identity(
    conn: Connection,
    *,
    library_id: int,
    canonical_path: str | Path,
    fs_device_id: str | int,
    fs_object_id: str | int,
    observed_at: str,
) -> None:
    """Append/re-observe one source directory identity without ever forgetting it."""
    conn.execute(
        """
        INSERT INTO library_directory_identities (
            library_id,canonical_path,fs_device_id,fs_object_id,
            first_observed_at,last_verified_at
        ) VALUES (?,?,?,?,?,?)
        ON CONFLICT(library_id,fs_device_id,fs_object_id) DO UPDATE SET
            canonical_path=excluded.canonical_path,
            last_verified_at=excluded.last_verified_at
        """,
        (
            int(library_id),
            _canonical(Path(canonical_path)),
            str(fs_device_id),
            str(fs_object_id),
            observed_at,
            observed_at,
        ),
    )
