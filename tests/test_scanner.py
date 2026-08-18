import time
from pathlib import Path

from PIL import Image

from ppa.db import connect
from ppa.scanner import scan_library


def _make_image(path: Path, size: tuple[int, int] = (40, 30), color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)


def test_new_files_are_catalogued(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _make_image(library / "IMG_0001.jpg")
    _make_image(library / "sub" / "IMG_0002.png")

    conn = connect(tmp_path / "catalogue.sqlite3")
    report = scan_library(conn, library)

    assert report.new_files == 2
    assert report.files_scanned == 2

    files = conn.execute("SELECT * FROM files ORDER BY filename").fetchall()
    assert len(files) == 2
    assert files[0]["filename"] == "IMG_0001.jpg"
    assert files[0]["width_px"] == 40
    assert files[0]["height_px"] == 30
    assert files[0]["status"] == "active"


def test_unsupported_and_deferred_files_are_not_catalogued(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _make_image(library / "IMG_0001.jpg")
    (library / "notes.txt").parent.mkdir(parents=True, exist_ok=True)
    (library / "notes.txt").write_text("not a photo")
    (library / "IMG_0002.CR2").write_bytes(b"fake raw data")

    conn = connect(tmp_path / "catalogue.sqlite3")
    report = scan_library(conn, library)

    assert report.new_files == 1
    assert report.unsupported_files == 1       # notes.txt
    assert report.deferred_format_files == 1   # .CR2

    files = conn.execute("SELECT * FROM files").fetchall()
    assert len(files) == 1


def test_corrupt_file_is_reported_inaccessible_not_crashed(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    bad = library / "broken.jpg"
    bad.write_bytes(b"this is not a real jpeg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    report = scan_library(conn, library)

    assert report.new_files == 0
    assert len(report.inaccessible_files) == 1
    assert report.inaccessible_files[0][0] == str(bad)


def test_rescanning_unchanged_library_reports_known(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _make_image(library / "IMG_0001.jpg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    report = scan_library(conn, library)

    assert report.new_files == 0
    assert report.known_files == 1


def test_modified_file_is_detected_by_size_change(tmp_path: Path) -> None:
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    _make_image(img_path, size=(40, 30))

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    _make_image(img_path, size=(400, 300))  # much larger -> different size in bytes
    report = scan_library(conn, library)

    assert report.modified_files == 1
    row = conn.execute("SELECT * FROM files WHERE filename = 'IMG_0001.jpg'").fetchone()
    assert row["width_px"] == 400

    events = conn.execute(
        "SELECT * FROM integrity_events WHERE event_type = 'size_changed'"
    ).fetchall()
    assert len(events) == 1


def test_moved_file_same_name_same_size_is_tracked(tmp_path: Path) -> None:
    library = tmp_path / "library"
    original = library / "IMG_0001.jpg"
    _make_image(original)

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    new_location = library / "2004" / "IMG_0001.jpg"
    new_location.parent.mkdir(parents=True)
    original.rename(new_location)

    report = scan_library(conn, library)

    assert report.moved_files == 1
    assert report.missing_files == 0

    row = conn.execute("SELECT * FROM files WHERE filename = 'IMG_0001.jpg'").fetchone()
    assert row["path"] == str(new_location)
    assert row["status"] == "active"

    history = conn.execute(
        "SELECT path FROM file_path_history WHERE file_id = ? ORDER BY id", (row["id"],)
    ).fetchall()
    assert [h["path"] for h in history] == [str(original), str(new_location)]


def test_missing_file_is_marked_missing_not_deleted(tmp_path: Path) -> None:
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    _make_image(img_path)

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    img_path.unlink()
    report = scan_library(conn, library)

    assert report.missing_files == 1
    row = conn.execute("SELECT * FROM files WHERE filename = 'IMG_0001.jpg'").fetchone()
    assert row is not None  # never deleted from the catalogue
    assert row["status"] == "missing"


def test_scan_never_writes_to_source_files(tmp_path: Path) -> None:
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    _make_image(img_path)

    original_bytes = img_path.read_bytes()
    original_mtime = img_path.stat().st_mtime

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    assert img_path.read_bytes() == original_bytes
    assert img_path.stat().st_mtime == original_mtime
