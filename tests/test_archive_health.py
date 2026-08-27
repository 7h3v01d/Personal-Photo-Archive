"""Phase 12.0 — read-only Backup & Archive Health projection."""
from pathlib import Path
import shutil

from PIL import Image
import pytest

from ppa.archive_health import build_archive_health, build_archive_health_browse
from ppa.db import connect
from ppa.scanner import scan_library


def _img(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (41, 31), color).save(path)


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def test_archive_health_classifies_catalogue_copy_coverage_without_writes(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()

    _img(library / "single.jpg", (255, 0, 0))
    _img(library / "exact.jpg", (0, 255, 0)); _copy(library / "exact.jpg", library / "exact_copy.jpg")
    _img(library / "missing.jpg", (0, 0, 255))
    _img(library / "degraded.jpg", (255, 255, 0)); _copy(library / "degraded.jpg", library / "degraded_copy.jpg")
    _img(library / "divergent.jpg", (128, 0, 128)); _copy(library / "divergent.jpg", library / "divergent_copy.jpg")
    _img(library / "unknown.jpg", (0, 255, 255))
    _img(library / "unhealthy.jpg", (255, 0, 255))

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    # Create representative Phase-12 states using the established scanner:
    # one wholly missing Photo, one partially missing duplicate set, and one
    # logical Photo whose two current Files now have different bytes.
    (library / "missing.jpg").unlink()
    (library / "degraded_copy.jpg").unlink()
    _img(library / "divergent_copy.jpg", (255, 128, 0))
    scan_library(conn, library)

    unknown_id = conn.execute("SELECT id FROM files WHERE filename='unknown.jpg'").fetchone()["id"]
    unhealthy_id = conn.execute("SELECT id FROM files WHERE filename='unhealthy.jpg'").fetchone()["id"]
    unknown_revision = conn.execute("SELECT current_revision_id FROM files WHERE id=?", (unknown_id,)).fetchone()["current_revision_id"]
    conn.execute("UPDATE file_revisions SET sha256=NULL WHERE id=?", (unknown_revision,))
    conn.execute("UPDATE files SET health_status='hash_mismatch' WHERE id=?", (unhealthy_id,))
    conn.commit()

    before = conn.total_changes
    health = build_archive_health(conn, library_id=1)
    assert conn.total_changes == before
    assert health.read_only is True
    assert health.schema == "ppa-archive-health/2"
    assert health.total_photos == 7
    assert health.total_files == 10
    assert health.present_files == 8
    assert health.missing_files == 2
    assert health.no_present_count == 1
    assert health.single_present_count == 4
    assert health.multiple_exact_present_count == 1
    assert health.missing_copy_photo_count == 1
    assert health.unhealthy_present_count == 1
    assert health.unknown_hash_count == 1
    assert health.divergent_count == 1
    assert health.exact_storage_unknown_count == 0
    assert health.hardlink_overstated_count == 0
    assert health.distinct_file_object_count == 1
    # All tmp_path members live on one filesystem device in this test.
    assert health.distinct_device_count == 0
    assert health.attention_count == 6

    # Category browsers preserve logical-Photo identity and are also read-only.
    exact = build_archive_health_browse(conn, health, "multiple_exact")
    assert exact.read_only is True
    assert exact.total_members == 1
    assert exact.items[0].copy_count == 2
    attention = build_archive_health_browse(conn, health, "attention")
    assert attention.total_members == 6
    assert conn.total_changes == before
    conn.close()


def test_archive_health_refuses_unknown_library(tmp_path: Path) -> None:
    conn = connect(tmp_path / "catalogue.sqlite3")
    with pytest.raises(ValueError, match="unknown library"):
        build_archive_health(conn, library_id=999)
    conn.close()
