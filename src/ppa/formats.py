"""Supported image format registry.

Phase 1 priority formats: JPEG, PNG, TIFF, HEIC (where feasible).
RAW, WebP, and legacy formats are explicitly Phase 1+ later work — an
unrecognised extension is reported as unsupported, never silently ignored.

HEIC needs the optional `pillow-heif` plugin registered with Pillow before
`Image.open()` will understand it. If it isn't installed, HEIC files are
still *detected* (by extension) but reported as unsupported with a reason
that says why, rather than failing silently or crashing the scan.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("ppa.formats")

# extension (lowercase, with dot) -> canonical format label
CORE_EXTENSIONS: dict[str, str] = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}

HEIC_EXTENSIONS: dict[str, str] = {
    ".heic": "HEIC",
    ".heif": "HEIC",
}

_heif_registered = False


def _try_register_heif() -> bool:
    """Attempt to register the HEIF opener with Pillow. Safe to call more
    than once. Returns whether HEIC support is actually available.
    """
    global _heif_registered
    if _heif_registered:
        return True
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _heif_registered = True
        logger.info("HEIC support enabled via pillow-heif")
    except ImportError:
        logger.info(
            "pillow-heif not installed — HEIC/HEIF files will be reported "
            "as unsupported rather than scanned. Install the optional "
            "'heic' extra to enable them."
        )
    return _heif_registered


def supported_extensions() -> dict[str, str]:
    """Return the currently-supported extension -> format map, including
    HEIC only if the optional plugin is available.
    """
    extensions = dict(CORE_EXTENSIONS)
    if _try_register_heif():
        extensions.update(HEIC_EXTENSIONS)
    return extensions


def is_recognised_but_unsupported(extension: str) -> bool:
    """True for extensions we know about but can't currently read (e.g.
    HEIC without the plugin installed, or an explicitly deferred format).
    Distinct from a plain unknown extension, so the scan report can explain
    *why* a file was skipped.
    """
    ext = extension.lower()
    if ext in HEIC_EXTENSIONS and ext not in supported_extensions():
        return True
    return ext in DEFERRED_EXTENSIONS


# Known-but-not-yet-supported formats (Phase 1 "Later" list), so the scan
# report can say "deferred to a later phase" instead of just "unknown".
DEFERRED_EXTENSIONS: dict[str, str] = {
    ".raw": "RAW",
    ".cr2": "RAW (Canon)",
    ".cr3": "RAW (Canon)",
    ".nef": "RAW (Nikon)",
    ".arw": "RAW (Sony)",
    ".dng": "RAW (Adobe)",
    ".webp": "WebP",
    ".bmp": "Legacy (BMP)",
    ".gif": "Legacy (GIF)",
}
