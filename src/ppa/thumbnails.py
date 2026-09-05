"""Thumbnail generation and cache (Phase 4, lite).

Browsing a 10,000-photo library must never mean re-decoding full-resolution
originals on every scroll. This module renders small thumbnails once and
caches them on disk.

The cache is keyed by **content hash**, not path: two files that are
byte-identical (a photo and its backup copy) share a single thumbnail. That
falls straight out of the Phase 2 identity work. Files we haven't hashed yet
fall back to a path-based key.

Kept deliberately Qt-free so it can be tested on its own and reused
headlessly. Reads originals; writes only into the cache directory, never
near a source file. EXIF orientation is honoured so portrait shots from
older cameras don't display sideways.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from PIL import Image, ImageOps, UnidentifiedImageError

from ppa.hashing import sha256_file
from ppa.logging_setup import get_logger
from ppa.secure_write import (
    BoundDirectory, BoundTemporaryFile, SecureWriteError, atomic_write_bytes,
    ensure_directory_authority, is_windows_reparse_point_stat,
    windows_path_has_reparse_component,
)
from ppa.source_tree_authority import SourceTreeAuthorityError, SourceTreeAuthorityPolicy
from ppa.operational_authority import (
    OperationalAuthorityError, owned_file_identity, record_owned_file_identity,
    require_directory,
)

log = get_logger("thumbnails")

DEFAULT_SIZE = 256
THUMBNAIL_ATTESTATION_SCHEMA = "ppa-thumbnail-attestation/1"
THUMBNAIL_CACHE_MARKER = ".ppa-thumbnail-cache-v1"
THUMBNAIL_CACHE_MARKER_BYTES = b"PPA THUMBNAIL CACHE v1\n"


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


class ThumbnailAuthorityUnavailable(ValueError):
    """Thumbnail generation is disabled until source-tree authority is verified."""


@dataclass(frozen=True)
class ThumbnailAuthorityPolicy:
    """Catalogue-backed source-tree exclusion policy for thumbnail authority.

    Cache markers and cache-shaped filenames are operational metadata only.
    Writable cache authority is denied to *any historically observed directory
    object* from a registered Library tree, not merely the Library root.
    """

    forbidden_directory_identities: frozenset[tuple[str, str]]
    forbidden_roots: tuple[str, ...]

    @classmethod
    def from_connection(cls, conn: Connection) -> "ThumbnailAuthorityPolicy":
        try:
            policy = SourceTreeAuthorityPolicy.from_connection(conn)
        except SourceTreeAuthorityError as exc:
            raise ThumbnailAuthorityUnavailable(str(exc)) from exc
        return cls(policy.forbidden_directory_identities, policy.forbidden_roots)

    def validate_authority(self, authority) -> None:
        try:
            SourceTreeAuthorityPolicy(
                self.forbidden_directory_identities, self.forbidden_roots
            ).validate_authority(authority, purpose="thumbnail cache directory")
        except SourceTreeAuthorityError as exc:
            raise ValueError(str(exc)) from exc

_CACHE_ENTRY_RE = re.compile(
    r"^(?:[0-9a-f]{64}|path-[0-9a-f]{64})-\d+\.png"
    r"(?:\.attestation\.json)?(?:\.[0-9a-f]{32}\.(?:png\.tmp|tmp))?$"
)


class ThumbnailCache:
    def __init__(
        self,
        cache_dir: Path,
        size: int = DEFAULT_SIZE,
        *,
        conn: Connection | None = None,
        authority_policy: ThumbnailAuthorityPolicy | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.size = size
        if conn is not None and authority_policy is not None:
            raise ValueError("provide either conn or authority_policy, not both")
        if conn is not None:
            authority_policy = ThumbnailAuthorityPolicy.from_connection(conn)
        if authority_policy is None:
            # A caller that omits catalogue authority context must never gain a
            # writable cache merely because the target directory looks empty or
            # cache-shaped.  Tests with no registered Libraries pass an explicit
            # empty policy so the trust decision remains visible at the call site.
            raise ValueError(
                "thumbnail cache requires explicit registered-Library authority context"
            )
        self._authority_policy = authority_policy
        self._conn = conn
        self._session_owned_files: dict[str, tuple[int, int]] = {}
        authority = None
        try:
            # Bind/create each directory object and validate THAT exact object
            # against registered Library roots before it can create the next
            # child.  This closes both the initial root-substitution bypass and
            # the missing-cache-under-Library creation case.
            authority = ensure_directory_authority(
                self.cache_dir, validator=authority_policy.validate_authority
            )
            authority.verify_pathname()
            authority_policy.validate_authority(authority)
            if conn is not None:
                try:
                    require_directory(conn, "thumbnail_cache", authority, allow_multiple=True)
                except OperationalAuthorityError as exc:
                    raise ValueError(str(exc)) from exc
            self._cache_identity = tuple(authority.identity)

            # Marker/cache-shape inspection is operational hygiene only.  It is
            # never an authority credential; Library exclusion above is decisive.
            names = set(os.listdir(self.cache_dir))
            authority.verify_pathname()
            unexpected: list[str] = []
            for name in sorted(names):
                if name == THUMBNAIL_CACHE_MARKER or _CACHE_ENTRY_RE.fullmatch(name):
                    continue
                child = self.cache_dir / name
                # Forensic sub-caches are legitimate children.  Accept only a
                # real directory that already carries the exact cache marker.
                try:
                    nested_marker = child / THUMBNAIL_CACHE_MARKER
                    if child.is_dir() and not child.is_symlink() and nested_marker.is_file():
                        if nested_marker.read_bytes() == THUMBNAIL_CACHE_MARKER_BYTES:
                            continue
                except OSError:
                    pass
                unexpected.append(name)
            authority.verify_pathname()
            if unexpected:
                raise ValueError(
                    "thumbnail cache directory is not approved operational cache state"
                )

            marker = self.cache_dir / THUMBNAIL_CACHE_MARKER
            if marker.exists():
                authority.verify_pathname()
                try:
                    if marker.read_bytes() != THUMBNAIL_CACHE_MARKER_BYTES:
                        raise ValueError("thumbnail cache marker is invalid")
                finally:
                    authority.verify_pathname()
            else:
                # Legacy or newly-created clean cache: establish a durable marker
                # through the exact identity that was just validated.
                atomic_write_bytes(
                    marker, THUMBNAIL_CACHE_MARKER_BYTES,
                    prefix=THUMBNAIL_CACHE_MARKER + ".", suffix=".tmp",
                    replace=False, expected_parent_identity=self._cache_identity,
                )
                authority.verify_pathname()
        except (OSError, SecureWriteError) as exc:
            raise ValueError("thumbnail cache authority could not be established safely") from exc
        finally:
            if authority is not None:
                authority.close()

    def _safe_unlink_cache_entry(self, path: Path) -> None:
        """Best-effort cleanup without granting pathname-only delete authority.

        On POSIX there is no portable compare-and-unlink primitive for an exact
        inode.  Because cache cleanup is non-essential, a potentially raced child
        is deliberately left as debris rather than risking deletion of a source
        object substituted into the cache namespace.
        """
        path = Path(path)
        if path.parent != self.cache_dir:
            return
        expected = self._session_owned_files.get(_canonical(path))
        if expected is None and self._conn is not None:
            expected = owned_file_identity(self._conn, "thumbnail_cache_child", path)
        if expected is None:
            return
        # Bound verification prevents ordinary stale-path cleanup.  We still do
        # not unlink on POSIX because name->unlink lacks an inode CAS guarantee.
        try:
            with BoundDirectory.open(
                self.cache_dir, expected_identity=self._cache_identity
            ) as bound:
                st = bound.lstat_child_or_none(path.name)
                if st is None or (int(st.st_dev), int(st.st_ino)) != tuple(expected):
                    return
        except (SecureWriteError, OSError):
            return
        return

    def _install_cache_temp(
        self, temp: BoundTemporaryFile, destination: Path
    ) -> bool:
        """Install one generated cache child under identity-bearing ownership."""
        destination = Path(destination)
        record_path = _canonical(destination)
        if os.path.lexists(os.fspath(destination)):
            if self._conn is None:
                # No durable authority database: existing children are readable
                # cache state, never implicit replacement authority.
                return False
            expected = owned_file_identity(
                self._conn, "thumbnail_cache_child", destination
            )
            if expected is None:
                return False
            replace = True
        else:
            expected = None
            replace = False
        try:
            temp.install(
                destination, replace=replace,
                expected_existing_identity=expected,
            )
        except SecureWriteError:
            return False
        identity = tuple(temp.identity)
        self._session_owned_files[record_path] = identity
        if self._conn is not None:
            try:
                record_owned_file_identity(
                    self._conn, "thumbnail_cache_child", destination, identity,
                    canonical_path=record_path,
                )
            except Exception:
                # The installed object remains data-safe but untrusted.  Never
                # widen authority merely to clean it up or overwrite it later.
                self._session_owned_files.pop(record_path, None)
                return False
        return True

    def _key(self, path: Path, sha256: str | None) -> str:
        if sha256:
            return sha256
        # No hash available: derive a stable key from the absolute path.
        return "path-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()

    def cache_path(self, path: Path, sha256: str | None) -> Path:
        return self.cache_dir / f"{self._key(path, sha256)}-{self.size}.png"


    def cached_path_only(self, path: Path, sha256: str | None) -> Path | None:
        """Return an existing cached derivative without generating anything."""
        out = self.cache_path(path, sha256)
        return out if out.is_file() else None

    def _attestation_path(self, out: Path) -> Path:
        return out.with_name(out.name + ".attestation.json")

    def attested_cached_path(self, path: Path, sha256: str | None) -> Path | None:
        """Return a cache entry only when its Phase-12.3 attestation validates.

        Legacy cache files remain usable for ordinary browsing, but they are not
        promoted to forensic evidence merely because their filename contains a
        catalogue hash.
        """
        if not sha256:
            return None
        out = self.cache_path(path, sha256)
        att = self._attestation_path(out)
        if not out.is_file() or not att.is_file():
            return None
        try:
            data = json.loads(att.read_text(encoding="utf-8"))
            if data.get("schema") != THUMBNAIL_ATTESTATION_SCHEMA:
                return None
            if data.get("source_sha256") != sha256:
                return None
            if int(data.get("size", -1)) != int(self.size):
                return None
            if data.get("thumbnail_sha256") != sha256_file(out):
                return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return out

    def get_or_create_attested(self, path: Path, sha256: str) -> Path | None:
        """Return a derivative whose source identity has been proven now.

        This is intentionally stricter than :meth:`get_or_create`: the source is
        hashed before and after rendering and must equal ``sha256`` both times.
        It is used for forensic expected/current comparison, not bulk browsing.
        Source photos are read only; the PNG and its small attestation sidecar
        live solely in the cache directory.
        """
        path = Path(path)
        if not sha256:
            return None
        existing = self.attested_cached_path(path, sha256)
        if existing is not None:
            return existing
        try:
            if sha256_file(path) != sha256:
                return None
        except OSError:
            return None
        out = self.cache_path(path, sha256)
        rendered = self._render(path, out)
        if rendered is None:
            return None
        try:
            # Close the hash->decode TOCTOU window conservatively.  A file that
            # changes during rendering yields no attested derivative.
            if sha256_file(path) != sha256:
                self._safe_unlink_cache_entry(rendered)
                self._safe_unlink_cache_entry(self._attestation_path(rendered))
                return None
            thumb_sha = sha256_file(rendered)
        except OSError:
            self._safe_unlink_cache_entry(rendered)
            self._safe_unlink_cache_entry(self._attestation_path(rendered))
            return None
        att = self._attestation_path(rendered)
        payload = {
            "schema": THUMBNAIL_ATTESTATION_SCHEMA,
            "source_sha256": sha256,
            "thumbnail_sha256": thumb_sha,
            "size": int(self.size),
            "attested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        att_temp: BoundTemporaryFile | None = None
        try:
            att_temp = BoundTemporaryFile.create(
                att.parent, prefix=att.name + ".", suffix=".tmp",
                expected_parent_identity=self._cache_identity,
            )
            att_temp.write_bytes(
                (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            )
            if not self._install_cache_temp(att_temp, att):
                self._safe_unlink_cache_entry(rendered)
                return None
        except (OSError, SecureWriteError):
            self._safe_unlink_cache_entry(rendered)
            return None
        finally:
            if att_temp is not None:
                att_temp.cleanup()
        return rendered

    def get_or_create(self, path: Path, sha256: str | None = None) -> Path | None:
        """Return the path to a cached thumbnail PNG for ``path``, generating
        it if necessary. Returns None if the source can't be decoded.

        If a cached thumbnail already exists it is returned untouched — the
        original is not re-read.
        """
        out = self.cache_path(path, sha256)
        if out.exists():
            return out
        return self._render(Path(path), out)

    def _render(self, path: Path, out: Path) -> Path | None:
        temp: BoundTemporaryFile | None = None
        try:
            temp = BoundTemporaryFile.create(
                out.parent, prefix=out.name + ".", suffix=".png.tmp",
                expected_parent_identity=self._cache_identity,
            )
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img)  # honour camera orientation
                if img.mode not in ("RGB", "RGBA", "L", "LA"):
                    img = img.convert("RGB")
                img.thumbnail((self.size, self.size))
                with temp.binary_writer() as writer:
                    img.save(writer, format="PNG")
            temp.sync_and_verify()
            if not self._install_cache_temp(temp, out):
                return None
            return out
        except (UnidentifiedImageError, OSError, SecureWriteError) as exc:
            log.warning("Could not thumbnail %s (%s)", path, exc)
            return None
        finally:
            if temp is not None:
                temp.cleanup()
