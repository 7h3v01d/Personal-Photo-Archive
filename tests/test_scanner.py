import time
from pathlib import Path

import pytest
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


def test_modified_file_is_detected_by_content_change(tmp_path: Path) -> None:
    # Phase 2: modification is detected by SHA-256, recorded as
    # 'content_modified'. (Phase 1 detected it by size and called it
    # 'size_changed'; hashing supersedes that.)
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    _make_image(img_path, size=(40, 30))

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    _make_image(img_path, size=(400, 300))  # different pixels -> different bytes
    report = scan_library(conn, library)

    assert report.modified_files == 1
    row = conn.execute("SELECT * FROM files WHERE filename = 'IMG_0001.jpg'").fetchone()
    assert row["width_px"] == 400

    events = conn.execute(
        "SELECT * FROM integrity_events WHERE event_type = 'content_modified'"
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


# --- Phase 2: content-identity behaviour ------------------------------------


def _make_image_bytes(width: int, height: int, color: str = "red") -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="JPEG")
    return buf.getvalue()


def test_new_files_are_hashed(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _make_image(library / "IMG_0001.jpg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    report = scan_library(conn, library)

    assert report.new_files == 1
    assert report.hashed_files == 1
    row = conn.execute("SELECT sha256, hash_computed_at FROM files").fetchone()
    assert row["sha256"] is not None
    assert len(row["sha256"]) == 64  # hex sha-256
    assert row["hash_computed_at"] is not None


def test_same_size_edit_is_caught_by_hash(tmp_path: Path) -> None:
    # The capability Phase 1 could not have: an edit that keeps byte size
    # identical is still detected, because identity is content, not size.
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    library.mkdir()

    red = _make_image_bytes(40, 30, "red")
    blue = _make_image_bytes(40, 30, "blue")
    # The whole point of this test: same byte length, different content. If
    # this premise ever stops holding, fail loudly rather than passing for
    # the wrong reason (i.e. being caught by a size change after all).
    assert len(red) == len(blue)
    assert red != blue
    img_path.write_bytes(red)

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    img_path.write_bytes(blue)
    report = scan_library(conn, library)

    assert report.modified_files == 1
    events = conn.execute(
        "SELECT * FROM integrity_events WHERE event_type = 'content_modified'"
    ).fetchall()
    assert len(events) == 1


def test_moved_file_confirmed_by_hash_not_guessed(tmp_path: Path) -> None:
    library = tmp_path / "library"
    original = library / "IMG_0001.jpg"
    _make_image(original)

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    original_id = conn.execute("SELECT id FROM files").fetchone()["id"]

    new_location = library / "2004" / "renamed.jpg"  # different name AND folder
    new_location.parent.mkdir(parents=True)
    original.rename(new_location)

    report = scan_library(conn, library)

    assert report.moved_files == 1
    assert report.new_files == 0
    assert report.missing_files == 0

    row = conn.execute("SELECT * FROM files").fetchone()
    assert row["id"] == original_id  # same File identity, not a new row
    assert row["path"] == str(new_location)
    assert row["status"] == "active"

    events = conn.execute(
        "SELECT event_type FROM integrity_events WHERE file_id = ?", (original_id,)
    ).fetchall()
    assert "move_confirmed" in {e["event_type"] for e in events}


def test_exact_duplicate_links_to_same_photo(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    data = _make_image_bytes(40, 30, "green")
    (library / "original.jpg").write_bytes(data)
    (library / "backup" ).mkdir()
    (library / "backup" / "copy.jpg").write_bytes(data)  # byte-identical

    conn = connect(tmp_path / "catalogue.sqlite3")
    report = scan_library(conn, library)

    assert report.new_files == 1
    assert report.duplicate_files == 1

    files = conn.execute("SELECT photo_id FROM files").fetchall()
    photos = conn.execute("SELECT id FROM photos").fetchall()
    assert len(files) == 2
    assert len({f["photo_id"] for f in files}) == 1  # both -> one logical Photo
    assert len(photos) == 1

    events = conn.execute(
        "SELECT * FROM integrity_events WHERE event_type = 'exact_duplicate'"
    ).fetchall()
    assert len(events) == 1


def test_missing_then_restored_is_tracked(tmp_path: Path) -> None:
    library = tmp_path / "library"
    img_path = library / "IMG_0001.jpg"
    _make_image(img_path)
    data = img_path.read_bytes()

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    file_id = conn.execute("SELECT id FROM files").fetchone()["id"]

    img_path.unlink()
    r_missing = scan_library(conn, library)
    assert r_missing.missing_files == 1
    assert conn.execute("SELECT status FROM files").fetchone()["status"] == "missing"

    # Same content reappears at a new path.
    restored_path = library / "recovered" / "IMG_0001.jpg"
    restored_path.parent.mkdir(parents=True)
    restored_path.write_bytes(data)

    r_restored = scan_library(conn, library)
    assert r_restored.restored_files == 1
    assert r_restored.missing_files == 0

    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    assert row["status"] == "active"
    assert row["path"] == str(restored_path)
    events = {
        e["event_type"]
        for e in conn.execute(
            "SELECT event_type FROM integrity_events WHERE file_id = ?", (file_id,)
        ).fetchall()
    }
    assert {"missing", "restored"} <= events


def test_phase1_catalogue_hashes_are_backfilled(tmp_path: Path) -> None:
    # Simulate a catalogue built before Phase 2 (sha256 NULL), then rescan.
    library = tmp_path / "library"
    _make_image(library / "IMG_0001.jpg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    conn.execute("UPDATE files SET sha256 = NULL, hash_computed_at = NULL")
    conn.commit()

    report = scan_library(conn, library)
    assert report.known_files == 1
    assert report.modified_files == 0  # backfill is not a modification
    row = conn.execute("SELECT sha256 FROM files").fetchone()
    assert row["sha256"] is not None


def test_unchanged_rescan_does_not_rehash(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _make_image(library / "IMG_0001.jpg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    first = scan_library(conn, library)
    assert first.hashed_files == 1  # hashed on first sight

    second = scan_library(conn, library)  # nothing touched
    assert second.known_files == 1
    assert second.hashed_files == 0  # trusted stored hash, no re-read


def test_library_root_filesystem_identity_is_persisted_and_not_silently_rebound(tmp_path: Path) -> None:
    import os
    from ppa.scanner import LibraryRootIdentityChangedError

    library = tmp_path / "library"
    _make_image(library / "a.jpg")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    row = conn.execute(
        "SELECT root_fs_device_id,root_fs_object_id,root_fs_verified_at FROM libraries"
    ).fetchone()
    assert row["root_fs_device_id"] is not None
    assert row["root_fs_object_id"] is not None
    assert row["root_fs_verified_at"] is not None
    original_identity = (row["root_fs_device_id"], row["root_fs_object_id"])

    parked = tmp_path / "library.parked"
    os.rename(library, parked)
    library.mkdir()
    _make_image(library / "replacement.jpg", color="blue")
    try:
        with pytest.raises(LibraryRootIdentityChangedError, match="different filesystem object|rebinding"):
            scan_library(conn, library)
    finally:
        # The failed scan must not redefine the stored source boundary.
        replacement = tmp_path / "replacement"
        os.rename(library, replacement)
        os.rename(parked, library)

    row2 = conn.execute(
        "SELECT root_fs_device_id,root_fs_object_id FROM libraries"
    ).fetchone()
    assert (row2["root_fs_device_id"], row2["root_fs_object_id"]) == original_identity


def test_library_root_filesystem_identity_cannot_be_silently_updated(tmp_path: Path) -> None:
    import sqlite3

    library = tmp_path / "library"
    _make_image(library / "a.jpg")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    row = conn.execute(
        "SELECT id,root_fs_device_id,root_fs_object_id FROM libraries"
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="immutable|rebind"):
        conn.execute(
            "UPDATE libraries SET root_fs_device_id=?,root_fs_object_id=? WHERE id=?",
            ("other-device", "other-object", row["id"]),
        )
    conn.rollback()
    row2 = conn.execute(
        "SELECT root_fs_device_id,root_fs_object_id FROM libraries WHERE id=?",
        (row["id"],),
    ).fetchone()
    assert (row2[0], row2[1]) == (row["root_fs_device_id"], row["root_fs_object_id"])



def test_source_tree_directory_identities_are_persisted_historically(tmp_path: Path) -> None:
    """Complete scans retain directory-object authority after a child moves away."""
    import os

    library = tmp_path / "library"
    child = library / "family" / "2004"
    empty = library / "empty"
    child.mkdir(parents=True)
    empty.mkdir(parents=True)
    _make_image(child / "a.jpg")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    lib = conn.execute(
        "SELECT id,source_tree_identity_complete,source_tree_identity_verified_at FROM libraries"
    ).fetchone()
    assert lib["source_tree_identity_complete"] == 1
    assert lib["source_tree_identity_verified_at"] is not None
    rows = conn.execute(
        "SELECT canonical_path,fs_device_id,fs_object_id FROM library_directory_identities "
        "WHERE library_id=? ORDER BY canonical_path", (lib["id"],)
    ).fetchall()
    paths = {r["canonical_path"] for r in rows}
    expected = {
        os.path.normcase(os.path.realpath(str(library))),
        os.path.normcase(os.path.realpath(str(library / "family"))),
        os.path.normcase(os.path.realpath(str(child))),
        os.path.normcase(os.path.realpath(str(empty))),
    }
    assert expected.issubset(paths)
    child_identity = next(
        (r["fs_device_id"], r["fs_object_id"]) for r in rows
        if r["canonical_path"] == os.path.normcase(os.path.realpath(str(child)))
    )

    moved = tmp_path / "moved-2004"
    os.rename(child, moved)
    scan_library(conn, library)
    retained = conn.execute(
        "SELECT 1 FROM library_directory_identities "
        "WHERE library_id=? AND fs_device_id=? AND fs_object_id=?",
        (lib["id"], *child_identity),
    ).fetchone()
    assert retained is not None


def test_source_tree_authority_is_incomplete_until_successful_scan(tmp_path: Path) -> None:
    """Migration-038 state is not permission before a complete directory inventory."""
    import os
    from ppa.source_tree_authority import SourceTreeAuthorityError, SourceTreeAuthorityPolicy

    library = tmp_path / "library"
    library.mkdir()
    conn = connect(tmp_path / "catalogue.sqlite3")
    # Directly registered row simulates an upgraded/unfinished catalogue before rescan.
    st = library.stat()
    conn.execute(
        "INSERT INTO libraries (root_display_path,root_canonical_path,root_fs_device_id,root_fs_object_id,root_fs_verified_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        (str(library), os.path.normcase(os.path.realpath(str(library))), str(st.st_dev), str(st.st_ino)),
    )
    conn.commit()
    with pytest.raises(SourceTreeAuthorityError, match="not completely verified|rescan"):
        SourceTreeAuthorityPolicy.from_connection(conn)
