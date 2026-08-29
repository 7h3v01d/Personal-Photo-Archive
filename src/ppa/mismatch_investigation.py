"""Phase 12.3 — read-only forensic investigation of verified hash mismatches.

A File whose current bytes disagree with its immutable current FileRevision has
*two different truths* that must not be collapsed:

* expected/catalogued identity — the SHA-256 recorded on the FileRevision;
* current on-disk observation — bytes that Verify proved do not match it.

This module builds an explicit comparison without mutating catalogue authority or
source files.  Derivative PNGs may be written below the thumbnail cache only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from sqlite3 import Connection

from PIL import Image, UnidentifiedImageError

from ppa.hashing import sha256_file
from ppa.thumbnails import ThumbnailCache

MISMATCH_INVESTIGATION_SCHEMA = "ppa-mismatch-investigation/1"


@dataclass(frozen=True)
class MismatchInvestigation:
    schema: str
    read_only: bool
    file_id: str
    photo_id: str
    filename: str
    path: str
    health_status: str
    expected_revision_id: str
    expected_sha256: str
    verify_observation_id: int | None
    verify_observed_sha256: str | None
    verify_observed_at: str | None
    current_observed_sha256: str | None
    current_state: str
    expected_reference_path: str | None
    expected_reference_status: str
    expected_reference_file_id: str | None
    current_preview_path: str | None
    latest_resolution_action: str | None
    latest_resolution_at: str | None
    latest_resolution_note: str | None
    notes: tuple[str, ...]

    @property
    def expected_reference_attested(self) -> bool:
        return self.expected_reference_status in {
            "attested_cache", "confirmed_exact_copy", "current_revalidated"
        }

    def to_dict(self) -> dict:
        return asdict(self)


def _latest_verify_observation(conn: Connection, file_id: str, expected_revision_id: str):
    return conn.execute(
        """
        SELECT id, observed_sha256, observed_at
          FROM integrity_mismatch_observations
         WHERE file_id=? AND expected_revision_id=?
         ORDER BY id DESC
         LIMIT 1
        """,
        (file_id, expected_revision_id),
    ).fetchone()


def _decodable(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (UnidentifiedImageError, OSError):
        return False


def build_mismatch_investigation(
    conn: Connection,
    file_id: str,
    *,
    thumbnail_cache_dir: Path,
    expected_size: int = 256,
    current_preview_size: int = 640,
) -> MismatchInvestigation:
    """Build one forensic mismatch comparison.

    Source photographs are only read.  The catalogue is not written.  Any PNG
    materialisation is confined to ``thumbnail_cache_dir``.
    """
    row = conn.execute(
        """
        SELECT f.id, f.photo_id, f.filename, f.path, f.health_status,
               f.current_revision_id, r.sha256 AS expected_sha256
          FROM files f
          LEFT JOIN file_revisions r ON r.id=f.current_revision_id
         WHERE f.id=?
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown File")
    if not row["current_revision_id"] or not row["expected_sha256"]:
        raise ValueError("File has no hashed current FileRevision to investigate")

    expected_sha = str(row["expected_sha256"])
    path = Path(row["path"])
    latest = _latest_verify_observation(conn, file_id, row["current_revision_id"])
    verify_observation_id = int(latest["id"]) if latest else None
    verify_sha = latest["observed_sha256"] if latest else None
    verify_at = latest["observed_at"] if latest else None
    latest_resolution = conn.execute(
        "SELECT action,note,resolved_at FROM integrity_mismatch_resolutions "
        "WHERE file_id=? AND expected_revision_id=? ORDER BY id DESC LIMIT 1",
        (file_id, row["current_revision_id"]),
    ).fetchone()
    notes: list[str] = []

    current_sha: str | None = None
    current_preview: Path | None = None
    if not path.is_file():
        current_state = "missing"
        notes.append("The recorded path is not currently available; no current-byte preview was produced.")
    elif not _decodable(path):
        current_state = "unreadable"
        notes.append("The current file cannot be decoded as an image.")
        try:
            current_sha = sha256_file(path)
        except OSError:
            current_sha = None
    else:
        try:
            current_sha = sha256_file(path)
        except OSError:
            current_sha = None
            current_state = "unreadable"
            notes.append("The current file could not be hashed during investigation.")
        else:
            current_state = "matches_expected" if current_sha == expected_sha else "still_mismatched"
            current_cache = ThumbnailCache(
                Path(thumbnail_cache_dir) / "forensic-current",
                size=current_preview_size,
            )
            current_preview = current_cache.get_or_create_attested(path, current_sha)
            if current_preview is None:
                notes.append("Current bytes changed while the forensic preview was being established, or could not be rendered.")

    expected_cache = ThumbnailCache(Path(thumbnail_cache_dir), size=expected_size)
    expected_reference = expected_cache.attested_cached_path(path, expected_sha)
    reference_status = "attested_cache" if expected_reference is not None else "unavailable"
    reference_file_id: str | None = None

    # If the current bytes now reproduce the expected identity, they may safely
    # re-attest the catalogue-keyed derivative.  Database health remains untouched;
    # only a subsequent Verify may clear the mismatch flag.
    if expected_reference is None and current_sha == expected_sha and path.is_file():
        expected_reference = expected_cache.get_or_create_attested(path, expected_sha)
        if expected_reference is not None:
            reference_status = "current_revalidated"
            reference_file_id = file_id
            notes.append(
                "Current bytes now hash to the catalogue identity. The File remains flagged until Verify is run again."
            )

    # Otherwise, a *different* present File with the expected revision hash can
    # reconstruct the expected visual reference only after being re-hashed now.
    if expected_reference is None:
        candidates = conn.execute(
            """
            SELECT f.id, f.path
              FROM files f
              JOIN file_revisions r ON r.id=f.current_revision_id
             WHERE f.id<>?
               AND f.presence_status='present'
               AND f.health_status='ok'
               AND r.sha256=?
             ORDER BY f.id
            """,
            (file_id, expected_sha),
        ).fetchall()
        for candidate in candidates:
            candidate_path = Path(candidate["path"])
            if not candidate_path.is_file():
                continue
            established = expected_cache.get_or_create_attested(candidate_path, expected_sha)
            if established is not None:
                expected_reference = established
                reference_status = "confirmed_exact_copy"
                reference_file_id = candidate["id"]
                break

    # Legacy cache files pre-date Phase 12.3 attestations.  They can be useful
    # visual context, but cannot be called trusted forensic evidence because an
    # older cache miss could have rendered changed bytes under the catalogue key.
    if expected_reference is None:
        legacy = expected_cache.cached_path_only(path, expected_sha)
        if legacy is not None:
            expected_reference = legacy
            reference_status = "legacy_unattested_cache"
            notes.append(
                "The catalogue-keyed thumbnail predates derivative attestation. It is shown only as untrusted legacy context."
            )
        else:
            notes.append(
                "No attested expected-image derivative is available. PPA will not recreate it from mismatching current bytes."
            )

    if verify_sha and current_sha and verify_sha != current_sha:
        notes.append(
            "The on-disk bytes changed again after the most recent Verify mismatch observation."
        )

    return MismatchInvestigation(
        schema=MISMATCH_INVESTIGATION_SCHEMA,
        read_only=True,
        file_id=row["id"],
        photo_id=row["photo_id"],
        filename=row["filename"],
        path=row["path"],
        health_status=row["health_status"],
        expected_revision_id=row["current_revision_id"],
        expected_sha256=expected_sha,
        verify_observation_id=verify_observation_id,
        verify_observed_sha256=verify_sha,
        verify_observed_at=verify_at,
        current_observed_sha256=current_sha,
        current_state=current_state,
        expected_reference_path=str(expected_reference) if expected_reference else None,
        expected_reference_status=reference_status,
        expected_reference_file_id=reference_file_id,
        current_preview_path=str(current_preview) if current_preview else None,
        latest_resolution_action=latest_resolution["action"] if latest_resolution else None,
        latest_resolution_at=latest_resolution["resolved_at"] if latest_resolution else None,
        latest_resolution_note=latest_resolution["note"] if latest_resolution else None,
        notes=tuple(notes),
    )
