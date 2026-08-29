"""Phase 12.3 — expected/catalogued vs current untrusted bytes forensics."""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

from ppa.db import connect, current_schema_version
from ppa.hashing import sha256_file
from ppa.integrity import verify_library
from ppa.mismatch_investigation import (
    MISMATCH_INVESTIGATION_SCHEMA,
    build_mismatch_investigation,
)
from ppa.scanner import scan_library
from ppa.thumbnails import ThumbnailCache


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def _file_row(conn, path: Path):
    return conn.execute(
        "SELECT f.id, f.current_revision_id, r.sha256 AS sha "
        "FROM files f JOIN file_revisions r ON r.id=f.current_revision_id "
        "WHERE f.path=?",
        (str(path),),
    ).fetchone()


def test_mismatch_investigation_uses_attested_expected_and_fresh_current(tmp_path: Path) -> None:
    library = tmp_path / "library"
    image = library / "one.jpg"
    _img(image, "red")
    db = tmp_path / "catalogue.sqlite3"
    conn = connect(db)
    scan_library(conn, library)
    row = _file_row(conn, image)
    expected_sha = row["sha"]
    cache_dir = tmp_path / "thumbs"

    cache = ThumbnailCache(cache_dir, size=256)
    expected_png = cache.get_or_create_attested(image, expected_sha)
    assert expected_png is not None

    _img(image, "blue")
    verify_library(conn)
    inv = build_mismatch_investigation(
        conn, row["id"], thumbnail_cache_dir=cache_dir
    )

    assert inv.schema == MISMATCH_INVESTIGATION_SCHEMA
    assert inv.read_only is True
    assert inv.current_state == "still_mismatched"
    assert inv.expected_sha256 == expected_sha
    assert inv.current_observed_sha256 != expected_sha
    assert inv.verify_observed_sha256 == inv.current_observed_sha256
    assert inv.expected_reference_status == "attested_cache"
    assert inv.expected_reference_attested is True
    assert Path(inv.expected_reference_path) == expected_png
    assert inv.current_preview_path is not None
    assert Path(inv.current_preview_path).is_file()
    # Expected and current derivatives are visually/content-distinct artifacts.
    assert sha256_file(Path(inv.expected_reference_path)) != sha256_file(Path(inv.current_preview_path))
    # Investigation never adopts suspect bytes as the FileRevision truth.
    assert conn.execute(
        "SELECT sha256 FROM file_revisions WHERE id=?", (row["current_revision_id"],)
    ).fetchone()["sha256"] == expected_sha


def test_legacy_catalogue_cache_is_shown_but_not_called_attested(tmp_path: Path) -> None:
    library = tmp_path / "library"
    image = library / "one.jpg"
    _img(image, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    row = _file_row(conn, image)
    cache_dir = tmp_path / "thumbs"
    cache = ThumbnailCache(cache_dir, size=256)
    legacy = cache.get_or_create(image, row["sha"])
    assert legacy is not None
    assert cache.attested_cached_path(image, row["sha"]) is None

    _img(image, "blue")
    verify_library(conn)
    inv = build_mismatch_investigation(conn, row["id"], thumbnail_cache_dir=cache_dir)

    assert inv.expected_reference_status == "legacy_unattested_cache"
    assert inv.expected_reference_attested is False
    assert Path(inv.expected_reference_path) == legacy
    # Critically, the mismatching source was not used to manufacture an
    # attestation for the trusted catalogue hash.
    assert cache.attested_cached_path(image, row["sha"]) is None


def test_exact_copy_can_reestablish_expected_visual_reference(tmp_path: Path) -> None:
    library = tmp_path / "library"
    suspect = library / "a.jpg"
    good_copy = library / "b.jpg"
    _img(suspect, "red")
    shutil.copy2(suspect, good_copy)
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    bad_row = _file_row(conn, suspect)
    good_row = _file_row(conn, good_copy)
    assert bad_row["sha"] == good_row["sha"]

    _img(suspect, "blue")
    verify_library(conn)
    cache_dir = tmp_path / "thumbs"
    inv = build_mismatch_investigation(conn, bad_row["id"], thumbnail_cache_dir=cache_dir)

    assert inv.current_state == "still_mismatched"
    assert inv.expected_reference_status == "confirmed_exact_copy"
    assert inv.expected_reference_file_id == good_row["id"]
    assert inv.expected_reference_attested is True
    assert Path(inv.expected_reference_path).is_file()
    assert ThumbnailCache(cache_dir, size=256).attested_cached_path(
        good_copy, bad_row["sha"]
    ) is not None


def test_current_bytes_matching_again_do_not_clear_health_without_verify(tmp_path: Path) -> None:
    library = tmp_path / "library"
    image = library / "one.jpg"
    _img(image, "red")
    original = image.read_bytes()
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    row = _file_row(conn, image)

    _img(image, "blue")
    verify_library(conn)
    assert conn.execute("SELECT health_status FROM files WHERE id=?", (row["id"],)).fetchone()[0] == "hash_mismatch"

    image.write_bytes(original)
    inv = build_mismatch_investigation(
        conn, row["id"], thumbnail_cache_dir=tmp_path / "thumbs"
    )
    assert inv.current_state == "matches_expected"
    assert inv.expected_reference_status == "current_revalidated"
    assert inv.expected_reference_attested is True
    # Forensics is read-only catalogue-wise. Verify remains the authority that
    # reconciles current health state.
    assert conn.execute("SELECT health_status FROM files WHERE id=?", (row["id"],)).fetchone()[0] == "hash_mismatch"


def test_migration_029_is_present(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")
    assert current_schema_version(conn) >= 29
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='integrity_mismatch_observations'"
    ).fetchone() is not None
