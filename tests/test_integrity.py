import io
from pathlib import Path

from PIL import Image

from ppa.db import connect
from ppa.integrity import verify_library
from ppa.scanner import scan_library


def _make_image(path: Path, size=(40, 30), color="red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)


def test_verify_reports_ok_for_untouched_library(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _make_image(library / "IMG_0001.jpg")
    _make_image(library / "IMG_0002.jpg", color="blue")

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    report = verify_library(conn)
    assert report.verified_ok == 2
    assert report.mismatches == 0
    assert report.problems == []


def test_verify_flags_silent_corruption_without_repairing(tmp_path: Path) -> None:
    # A file whose bytes change while size/mtime are restored would slip past
    # a scan's fast path; verify re-hashes and catches it.
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    _make_image(img_path)

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    stored = conn.execute("SELECT sha256 FROM files").fetchone()["sha256"]

    # Corrupt the content while keeping a valid, decodable image.
    _make_image(img_path, color="blue")

    report = verify_library(conn)
    assert report.mismatches == 1
    # Stored hash is deliberately NOT overwritten.
    assert conn.execute("SELECT sha256 FROM files").fetchone()["sha256"] == stored

    events = conn.execute(
        "SELECT * FROM integrity_events WHERE event_type = 'hash_mismatch'"
    ).fetchall()
    assert len(events) == 1


def test_verify_marks_missing_files(tmp_path: Path) -> None:
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    _make_image(img_path)

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    img_path.unlink()
    report = verify_library(conn)
    assert report.now_missing == 1
    assert conn.execute("SELECT status FROM files").fetchone()["status"] == "missing"


def test_verify_backfills_missing_hash(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _make_image(library / "IMG_0001.jpg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    conn.execute("UPDATE files SET sha256 = NULL, hash_computed_at = NULL")
    conn.commit()

    report = verify_library(conn)
    assert report.backfilled == 1
    assert conn.execute("SELECT sha256 FROM files").fetchone()["sha256"] is not None


def test_verify_flags_unreadable_file_as_corrupt(tmp_path: Path) -> None:
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    _make_image(img_path)

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    img_path.write_bytes(b"no longer a valid image")
    report = verify_library(conn)
    assert report.corrupt == 1
    events = conn.execute(
        "SELECT * FROM integrity_events WHERE event_type = 'corrupt'"
    ).fetchall()
    assert len(events) == 1
