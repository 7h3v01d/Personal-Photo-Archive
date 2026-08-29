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
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ppa.hashing import sha256_file
from ppa.logging_setup import get_logger

log = get_logger("thumbnails")

DEFAULT_SIZE = 256
THUMBNAIL_ATTESTATION_SCHEMA = "ppa-thumbnail-attestation/1"


class ThumbnailCache:
    def __init__(self, cache_dir: Path, size: int = DEFAULT_SIZE) -> None:
        self.cache_dir = Path(cache_dir)
        self.size = size
        self.cache_dir.mkdir(parents=True, exist_ok=True)

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
                rendered.unlink(missing_ok=True)
                self._attestation_path(rendered).unlink(missing_ok=True)
                return None
            thumb_sha = sha256_file(rendered)
        except OSError:
            rendered.unlink(missing_ok=True)
            self._attestation_path(rendered).unlink(missing_ok=True)
            return None
        att = self._attestation_path(rendered)
        payload = {
            "schema": THUMBNAIL_ATTESTATION_SCHEMA,
            "source_sha256": sha256,
            "thumbnail_sha256": thumb_sha,
            "size": int(self.size),
            "attested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        tmp = att.with_name(att.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(att)
        except OSError:
            tmp.unlink(missing_ok=True)
            rendered.unlink(missing_ok=True)
            return None
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
        try:
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img)  # honour camera orientation
                if img.mode not in ("RGB", "RGBA", "L", "LA"):
                    img = img.convert("RGB")
                img.thumbnail((self.size, self.size))
                tmp = out.with_suffix(".png.tmp")
                img.save(tmp, format="PNG")
                tmp.replace(out)  # atomic-ish: never leave a half-written png
            return out
        except (UnidentifiedImageError, OSError) as exc:
            log.warning("Could not thumbnail %s (%s)", path, exc)
            return None
