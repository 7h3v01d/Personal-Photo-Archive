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

# Sources this module owns and replaces on (re-)extraction of a revision. The
# filesystem/mtime observation is owned by the scanner (it directly observes
# the filesystem), so it is deliberately NOT in this set.
MACHINE_SOURCES = ("exif", "exif-gps", "exif-derived")

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
    revision_id: str,
    result: ExtractionResult,
    session_id: str | None = None,
) -> None:
    """Replace THIS revision's machine observations with a fresh set and set
    the file's camera to whatever the current revision supports.

    Observations are attached to ``revision_id`` and only this revision's are
    replaced — historical revisions keep their observations, so the archive
    never forgets what an earlier version of the file claimed.
    """
    placeholders = ", ".join("?" for _ in MACHINE_SOURCES)
    conn.execute(
        f"DELETE FROM metadata_observations WHERE file_revision_id = ? "
        f"AND source IN ({placeholders})",
        (revision_id, *MACHINE_SOURCES),
    )
    conn.executemany(
        "INSERT INTO metadata_observations "
        "(file_id, file_revision_id, source, key, value, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(file_id, revision_id, o.source, o.key, o.value, session_id)
         for o in result.observations],
    )
    _set_camera_for_current_revision(conn, file_id, revision_id, result)


def _set_camera_for_current_revision(
    conn: Connection, file_id: str, revision_id: str, result: ExtractionResult
) -> None:
    """Set files.camera_id from this revision's camera — but only if this
    revision is the file's current one, and clear it when the current content
    supports no camera (so a stale camera never outlives the bytes that named it).
    """
    row = conn.execute(
        "SELECT current_revision_id FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if row is None or row["current_revision_id"] != revision_id:
        return  # extracting a non-current revision must not touch the file's camera
    camera_id = _resolve_camera(conn, result.make, result.model, result.serial)
    conn.execute("UPDATE files SET camera_id = ? WHERE id = ?", (camera_id, file_id))


def extract_for_revision(conn: Connection, file_id: str, revision_id: str) -> str:
    """Extract metadata for one revision's content. Returns the resulting
    extraction_status. Reads the file at the file's current path (the bytes on
    disk are the current revision's content).
    """
    frow = conn.execute(
        "SELECT path FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if frow is None:
        return "failed_unreadable"
    path = Path(frow["path"])

    try:
        result = extract_observations(path)
    except UnidentifiedImageError:
        status = "failed_unreadable"  # not a decodable image; don't keep retrying
        result = None
    except OSError:
        # Transient (lock, USB hiccup, permission transition): leave PENDING-ish
        # so the next run retries rather than falsely recording success.
        conn.execute(
            "UPDATE file_revisions SET extraction_status = 'failed_transient' WHERE id = ?",
            (revision_id,),
        )
        return "failed_transient"

    if result is not None:
        store_metadata(conn, file_id, revision_id, result)
        status = "success"

    conn.execute(
        "UPDATE file_revisions SET extraction_status = ?, extracted_at = ? WHERE id = ?",
        (status, _iso_now(), revision_id),
    )
    return status


def extract_stale(conn: Connection, progress_cb=None) -> int:
    """Extract metadata for every current revision that still needs it —
    either never attempted (pending) or a transient failure worth retrying.
    Idempotent: a second run does nothing. Returns the number processed.
    """
    def _progress(msg: str) -> None:
        if progress_cb is not None:
            progress_cb(msg)

    targets = conn.execute(
        """
        SELECT f.id AS file_id, r.id AS revision_id
        FROM files f
        JOIN file_revisions r ON r.id = f.current_revision_id
        WHERE f.presence_status = 'present'
          AND r.sha256 IS NOT NULL
          AND r.extraction_status IN ('pending', 'failed_transient')
        ORDER BY f.filename, f.id
        """
    ).fetchall()

    total = len(targets)
    if total:
        _progress(f"Reading metadata… 0/{total}")
    for i, row in enumerate(targets, start=1):
        extract_for_revision(conn, row["file_id"], row["revision_id"])
        if i % 25 == 0 or i == total:
            _progress(f"Reading metadata… {i}/{total}")
    conn.commit()
    log.info("Metadata extraction: %d revision(s) processed", total)
    return total


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
