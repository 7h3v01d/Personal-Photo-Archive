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
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ppa.logging_setup import get_logger

log = get_logger("thumbnails")

DEFAULT_SIZE = 256


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
