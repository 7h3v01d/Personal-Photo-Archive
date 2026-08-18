"""Metadata extraction (Phase 3).

Reads embedded EXIF (plus a filesystem date) and records it as
**observations**, never as truth. An observation is a (source, key, value)
triple: "the EXIF sub-IFD says DateTimeOriginal is 2004:12:25 09:14:32" is a
fact about what the file *claims*, not a ruling on when the photo was taken.
That distinction is the whole point of the archive — the interpreted capture
date, its confidence and evidence, live in a later phase (6/7) and never
overwrite what was observed here.

Nothing in this module writes to a source file. It only reads bytes and
writes rows into the catalogue's metadata_observations / cameras tables.

Reproducibility (Phase 0 rule 8): extraction is idempotent and hash-aware.
Each file carries two marker observations — the extractor version and the
SHA-256 the metadata was read from. Re-running extraction replaces the
machine-derived observations for a file rather than piling up duplicates,
and a file is only re-read when its content hash changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Connection

from PIL import ExifTags, Image, UnidentifiedImageError
from PIL.TiffImagePlugin import IFDRational

from ppa.logging_setup import get_logger

log = get_logger("metadata")

EXTRACTOR_VERSION = "pillow-exif/1"

# Sources this module owns. Re-extraction replaces exactly these and never
# touches anything else (e.g. future user-entered observations).
MACHINE_SOURCES = ("exif", "exif-gps", "exif-derived", "filesystem", "meta")

# EXIF tags that are large/binary/noise — recorded nowhere, so the catalogue
# stays readable and small.
_TAG_BLOCKLIST = {
    "MakerNote",
    "PrintImageMatching",
    "ImageResources",
    "XMLPacket",
    "InteroperabilityIndex",
}

_MAX_VALUE_LEN = 512


@dataclass(frozen=True)
class Observation:
    source: str
    key: str
    value: str


@dataclass
class ExtractionResult:
    observations: list[Observation] = field(default_factory=list)
    make: str | None = None
    model: str | None = None
    serial: str | None = None


def _stringify(value) -> str | None:
    """Render an EXIF value as a compact string, or None to skip it."""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8").replace("\x00", "").strip()
        except UnicodeDecodeError:
            return None  # binary blob — not a useful observation
        return text or None
    if isinstance(value, IFDRational):
        if value.denominator == 0:
            return None
        if value.denominator == 1:
            return str(value.numerator)
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, tuple):
        parts = [_stringify(v) for v in value]
        return ", ".join(p for p in parts if p is not None) or None
    text = str(value).replace("\x00", "").strip()
    return text or None


def _dms_to_decimal(dms, ref) -> float | None:
    """Convert an (deg, min, sec) EXIF GPS tuple + N/S/E/W ref to decimal."""
    try:
        deg, minutes, seconds = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    decimal = deg + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        decimal = -decimal
    return round(decimal, 6)


def extract_observations(path: Path) -> ExtractionResult:
    """Read EXIF from ``path`` and return observations + parsed camera.

    Never raises on a missing/absent EXIF block — a file with no metadata
    simply yields an empty result. Only genuine read failures propagate as
    OSError to the caller.
    """
    result = ExtractionResult()

    try:
        with Image.open(path) as img:
            exif = img.getexif()
    except (UnidentifiedImageError, OSError):
        raise
    except Exception as exc:  # defensive: malformed EXIF shouldn't kill a scan
        log.warning("EXIF read failed for %s (%s)", path, exc)
        return result

    if not exif:
        return result

    # IFD0 (main image tags)
    for tag_id, raw in exif.items():
        name = ExifTags.TAGS.get(tag_id, f"Tag0x{tag_id:04X}")
        if name in _TAG_BLOCKLIST:
            continue
        value = _stringify(raw)
        if value and len(value) <= _MAX_VALUE_LEN:
            result.observations.append(Observation("exif", name, value))

    # Exif sub-IFD (DateTimeOriginal, ISO, exposure, lens, serial…)
    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
    except Exception:
        sub = {}
    for tag_id, raw in sub.items():
        name = ExifTags.TAGS.get(tag_id, f"Exif0x{tag_id:04X}")
        if name in _TAG_BLOCKLIST:
            continue
        value = _stringify(raw)
        if value and len(value) <= _MAX_VALUE_LEN:
            result.observations.append(Observation("exif", name, value))
        if name == "BodySerialNumber" and value:
            result.serial = value

    # GPS IFD
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:
        gps = {}
    gps_named: dict[str, object] = {}
    for tag_id, raw in gps.items():
        name = ExifTags.GPSTAGS.get(tag_id, f"GPS0x{tag_id:04X}")
        gps_named[name] = raw
        value = _stringify(raw)
        if value and len(value) <= _MAX_VALUE_LEN:
            result.observations.append(Observation("exif-gps", name, value))

    lat = _dms_to_decimal(gps_named.get("GPSLatitude"), gps_named.get("GPSLatitudeRef"))
    lon = _dms_to_decimal(gps_named.get("GPSLongitude"), gps_named.get("GPSLongitudeRef"))
    if lat is not None and lon is not None:
        result.observations.append(Observation("exif-derived", "GPSLatitudeDecimal", str(lat)))
        result.observations.append(Observation("exif-derived", "GPSLongitudeDecimal", str(lon)))

    make = _stringify(exif.get(0x010F))
    model = _stringify(exif.get(0x0110))
    result.make = make
    result.model = model
    return result


def _resolve_camera(conn: Connection, make, model, serial) -> int | None:
    if not any((make, model, serial)):
        return None
    row = conn.execute(
        "SELECT id FROM cameras WHERE make IS ? AND model IS ? AND serial IS ?",
        (make, model, serial),
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO cameras (make, model, serial) VALUES (?, ?, ?)",
        (make, model, serial),
    )
    return cur.lastrowid


def store_metadata(
    conn: Connection,
    file_id: str,
    result: ExtractionResult,
    sha256: str | None,
    fs_mtime: str | None,
    session_id: str | None = None,
) -> None:
    """Replace the machine-derived observations for ``file_id`` with a fresh
    set, link its camera, and stamp the extractor/hash markers.
    """
    placeholders = ", ".join("?" for _ in MACHINE_SOURCES)
    conn.execute(
        f"DELETE FROM metadata_observations WHERE file_id = ? AND source IN ({placeholders})",
        (file_id, *MACHINE_SOURCES),
    )

    rows = list(result.observations)
    if fs_mtime:
        rows.append(Observation("filesystem", "mtime", fs_mtime))
    # Reproducibility markers — what read this, and from which content.
    rows.append(Observation("meta", "_extractor", EXTRACTOR_VERSION))
    rows.append(Observation("meta", "_extracted_from_sha", sha256 or ""))

    conn.executemany(
        "INSERT INTO metadata_observations (file_id, source, key, value, session_id) "
        "VALUES (?, ?, ?, ?, ?)",
        [(file_id, o.source, o.key, o.value, session_id) for o in rows],
    )

    camera_id = _resolve_camera(conn, result.make, result.model, result.serial)
    if camera_id is not None:
        conn.execute("UPDATE files SET camera_id = ? WHERE id = ?", (camera_id, file_id))


def extract_for_file(conn: Connection, file_id: str, session_id: str | None = None) -> bool:
    """Extract and store metadata for one file. Returns True if it ran."""
    row = conn.execute(
        "SELECT path, sha256, fs_mtime, status FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if row is None or row["status"] != "active":
        return False

    path = Path(row["path"])
    try:
        result = extract_observations(path)
    except OSError as exc:
        log.warning("Could not read %s for metadata (%s)", path, exc)
        # Still stamp markers so we don't retry a genuinely unreadable file
        # every scan; a content change (new sha) will trigger a fresh attempt.
        store_metadata(conn, file_id, ExtractionResult(), row["sha256"], row["fs_mtime"], session_id)
        return True

    store_metadata(conn, file_id, result, row["sha256"], row["fs_mtime"], session_id)
    return True


def extract_stale(conn: Connection, progress_cb=None) -> int:
    """Extract metadata for every active file whose stored metadata doesn't
    match its current content hash (new files, or files whose bytes changed).

    Idempotent: running it twice in a row does no work the second time.
    Returns the number of files (re)processed.
    """
    def _progress(msg: str) -> None:
        if progress_cb is not None:
            progress_cb(msg)

    targets = conn.execute(
        """
        SELECT f.id FROM files f
        WHERE f.status = 'active' AND f.sha256 IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM metadata_observations o
              WHERE o.file_id = f.id
                AND o.source = 'meta'
                AND o.key = '_extracted_from_sha'
                AND o.value = f.sha256
          )
        ORDER BY f.filename, f.id
        """
    ).fetchall()

    total = len(targets)
    if total:
        _progress(f"Reading metadata… 0/{total}")
    for i, row in enumerate(targets, start=1):
        extract_for_file(conn, row["id"])
        if i % 25 == 0 or i == total:
            _progress(f"Reading metadata… {i}/{total}")
    conn.commit()
    log.info("Metadata extraction: %d file(s) processed", total)
    return total
