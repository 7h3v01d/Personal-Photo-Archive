"""Stable read-only observation of physical image bytes.

Phase 12.4.2 centralises the filesystem observation primitive used whenever a
catalogue action is about to rely on *current physical content*.  Catalogue
health/revision state is necessary evidence, but it cannot freeze an external
file between human review and execution.  These helpers therefore re-read the
source bytes without ever opening them for write.

The observation is deliberately conservative:
* stat before reading;
* SHA-256;
* Pillow decode/verify;
* SHA-256 again;
* stat again;
* require stable size/mtime/device/object identity and equal hashes.

A caller may then compare the stable SHA against the reviewed/current revision
SHA.  Failure is uncertainty, not evidence for some other identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, UnidentifiedImageError

from ppa.hashing import sha256_file


class PhysicalObservationError(ValueError):
    """Raised when current physical bytes cannot be stably attested."""


@dataclass(frozen=True)
class StableFileObservation:
    state: str
    sha256: str | None
    size_bytes: int | None
    mtime_ns: int | None
    fs_mtime: str | None
    fs_device_id: str | None
    fs_object_id: str | None
    width_px: int | None
    height_px: int | None
    mime_type: str | None


def _fmt_mtime(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _identity(stat_result) -> tuple[int, int, int, int]:
    return (
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        int(getattr(stat_result, "st_dev", 0)),
        int(getattr(stat_result, "st_ino", 0)),
    )


def observe_stable_image(
    path: Path, *, expected_sha256: str | None = None,
    hash_file: Callable[[Path], str] | None = None,
) -> StableFileObservation:
    """Return one stable, read-only physical observation of ``path``.

    Missing/unreadable content is represented as a state.  A source changing
    during the observation raises ``PhysicalObservationError`` because a mixed
    observation must never be used as identity evidence.
    """
    if hash_file is None:
        hash_file = sha256_file
    if not path.is_file():
        return StableFileObservation("missing", None, None, None, None, None, None, None, None, None)

    try:
        before = path.stat()
        first_sha = hash_file(path)
    except OSError:
        return StableFileObservation("unreadable", None, None, None, None, None, None, None, None, None)

    width = height = None
    mime = None
    decodable = True
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            mime = Image.MIME.get(image.format) if image.format else None
    except (UnidentifiedImageError, OSError, ValueError):
        decodable = False

    try:
        second_sha = hash_file(path)
        after = path.stat()
    except OSError as exc:
        raise PhysicalObservationError(
            "physical File changed while it was being re-attested; run Verify / refresh investigation"
        ) from exc

    if _identity(before) != _identity(after) or first_sha != second_sha:
        raise PhysicalObservationError(
            "physical File changed while it was being re-attested; run Verify / refresh investigation"
        )

    dev = getattr(after, "st_dev", None)
    obj = getattr(after, "st_ino", None)
    if not decodable:
        state = "unreadable"
    elif expected_sha256 is None:
        state = "observed"
    elif second_sha == expected_sha256:
        state = "matches_expected"
    else:
        state = "still_mismatched"

    return StableFileObservation(
        state=state,
        sha256=second_sha,
        size_bytes=int(after.st_size),
        mtime_ns=int(after.st_mtime_ns),
        fs_mtime=_fmt_mtime(after.st_mtime),
        fs_device_id=None if dev is None else str(dev),
        fs_object_id=None if obj is None else str(obj),
        width_px=width,
        height_px=height,
        mime_type=mime,
    )


def require_expected_physical_bytes(
    files: Iterable[tuple[str, str, str]],
    *,
    context: str = "identity operation",
) -> dict[str, StableFileObservation]:
    """Re-attest ``(file_id, path, expected_sha256)`` tuples fail-closed.

    Every File must be present, decodable, stably readable, and equal to the
    exact verified-current SHA bound to the reviewed catalogue state.
    """
    observed: dict[str, StableFileObservation] = {}
    for file_id, raw_path, expected_sha256 in files:
        try:
            current = observe_stable_image(Path(raw_path), expected_sha256=expected_sha256)
        except PhysicalObservationError as exc:
            raise PhysicalObservationError(
                f"{context}: physical File {file_id} could not be stably re-attested; "
                "run Verify / refresh investigation"
            ) from exc
        if current.state != "matches_expected" or current.sha256 != expected_sha256:
            raise PhysicalObservationError(
                f"{context}: physical File {file_id} changed since identity review; "
                "run Verify / refresh investigation"
            )
        observed[file_id] = current
    return observed
