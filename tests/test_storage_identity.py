"""Phase 12.1 — filesystem object identity / hard-link accounting."""
from __future__ import annotations

import os
from pathlib import Path
import shutil

from PIL import Image
import pytest

from ppa.archive_health import build_archive_health, build_archive_health_browse
from ppa.db import connect
from ppa.scanner import scan_library


def _img(path: Path, color=(120, 80, 40)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (37, 29), color).save(path)


def test_scan_records_current_storage_identity_and_sparse_history(tmp_path: Path) -> None:
    library = tmp_path / "library"
    source = library / "a.jpg"
    _img(source)
    conn = connect(tmp_path / "catalogue.sqlite3")

    first = scan_library(conn, library)
    row = conn.execute(
        "SELECT id, fs_device_id, fs_object_id, fs_link_count, "
        "fs_identity_observed_at, fs_identity_session FROM files"
    ).fetchone()
    assert row["fs_device_id"] is not None
    assert row["fs_object_id"] is not None
    assert int(row["fs_object_id"]) > 0
    assert row["fs_link_count"] >= 1
    assert row["fs_identity_observed_at"]
    assert row["fs_identity_session"] == first.session_id
    assert first.storage_identity_known == 1
    assert first.storage_identity_unknown == 0

    history = conn.execute(
        "SELECT reason, device_id, object_id, link_count FROM file_storage_identity_history "
        "WHERE file_id=? ORDER BY id", (row["id"],)
    ).fetchall()
    assert len(history) == 1
    assert history[0]["reason"] == "identity_established"
    assert (history[0]["device_id"], history[0]["object_id"]) == (
        row["fs_device_id"], row["fs_object_id"]
    )

    # Routine rescan refreshes current observation time/session but does not
    # append identical historical evidence forever.
    second = scan_library(conn, library)
    assert second.storage_identity_known == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM file_storage_identity_history WHERE file_id=?",
        (row["id"],),
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT fs_identity_session FROM files WHERE id=?", (row["id"],)
    ).fetchone()["fs_identity_session"] == second.session_id
    conn.close()


def test_archive_health_distinguishes_copy_from_hard_link(tmp_path: Path) -> None:
    library = tmp_path / "library"
    original = library / "original.jpg"
    _img(original)
    shutil.copyfile(original, library / "copy.jpg")

    hardlink = library / "hardlink.jpg"
    try:
        os.link(original, hardlink)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hard links unavailable on this test filesystem: {exc}")

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    health = build_archive_health(conn, library_id=1)

    # Three paths, one SHA, but only two filesystem objects because original +
    # hardlink are the same object while copy.jpg is a real second object.
    assert health.multiple_exact_present_count == 1
    assert health.exact_storage_unknown_count == 0
    assert health.hardlink_overstated_count == 1
    assert health.distinct_file_object_count == 1
    assert health.distinct_device_count == 0
    assert health.attention_count == 1  # path count is misleading until reviewed

    hardlinks = build_archive_health_browse(conn, health, "hardlinks")
    assert hardlinks.total_members == 1
    distinct = build_archive_health_browse(conn, health, "distinct_objects")
    assert distinct.total_members == 1

    rows = conn.execute(
        "SELECT filename, fs_device_id, fs_object_id, fs_link_count FROM files "
        "ORDER BY filename"
    ).fetchall()
    by_name = {r["filename"]: r for r in rows}
    assert (by_name["original.jpg"]["fs_device_id"], by_name["original.jpg"]["fs_object_id"]) == (
        by_name["hardlink.jpg"]["fs_device_id"], by_name["hardlink.jpg"]["fs_object_id"]
    )
    assert by_name["original.jpg"]["fs_object_id"] != by_name["copy.jpg"]["fs_object_id"]
    assert by_name["original.jpg"]["fs_link_count"] >= 2
    conn.close()


def test_archive_health_fails_closed_when_exact_set_storage_identity_is_incomplete(tmp_path: Path) -> None:
    library = tmp_path / "library"
    original = library / "a.jpg"
    _img(original)
    shutil.copyfile(original, library / "b.jpg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    b = conn.execute("SELECT id FROM files WHERE filename='b.jpg'").fetchone()["id"]

    # Simulate an upgraded catalogue / platform on which the latest object id
    # could not be established.  Archive Health must not reuse the other copy's
    # identity to manufacture certainty.
    conn.execute(
        "UPDATE files SET fs_object_id=NULL, fs_identity_observed_at=NULL, fs_identity_session=NULL WHERE id=?",
        (b,),
    )
    conn.commit()

    before = conn.total_changes
    health = build_archive_health(conn, library_id=1)
    assert conn.total_changes == before
    assert health.multiple_exact_present_count == 1
    assert health.exact_storage_unknown_count == 1
    assert health.hardlink_overstated_count == 0
    assert health.distinct_file_object_count == 0
    assert health.distinct_device_count == 0
    assert health.attention_count == 1
    assert build_archive_health_browse(conn, health, "storage_unknown").total_members == 1
    conn.close()


def test_distinct_device_classification_uses_only_complete_catalogue_evidence(tmp_path: Path) -> None:
    library = tmp_path / "library"
    original = library / "a.jpg"
    _img(original)
    shutil.copyfile(original, library / "b.jpg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    rows = conn.execute("SELECT id FROM files ORDER BY filename").fetchall()
    assert len(rows) == 2

    # The projection treats device IDs as opaque evidence.  Use two artificial
    # tokens here so the test does not require two mounted filesystems.
    conn.execute("UPDATE files SET fs_device_id='device-A' WHERE id=?", (rows[0]["id"],))
    conn.execute("UPDATE files SET fs_device_id='device-B' WHERE id=?", (rows[1]["id"],))
    conn.commit()

    health = build_archive_health(conn, library_id=1)
    assert health.exact_storage_unknown_count == 0
    assert health.distinct_file_object_count == 1
    assert health.distinct_device_count == 1
    assert health.hardlink_overstated_count == 0
    conn.close()


def test_storage_identity_history_records_same_bytes_object_replacement(tmp_path: Path) -> None:
    library = tmp_path / "library"
    source = library / "a.jpg"
    _img(source, (30, 60, 90))
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    file_row = conn.execute(
        "SELECT id, sha256, fs_device_id, fs_object_id FROM files"
    ).fetchone()
    old_key = (file_row["fs_device_id"], file_row["fs_object_id"])
    raw = source.read_bytes()

    # Create a second filesystem object while the original still exists, then
    # atomically replace the path. Bytes remain identical but object identity
    # must change and remain auditable.
    replacement = library / "replacement.tmp"
    replacement.write_bytes(raw)
    old_stat = source.stat()
    replacement_stat = replacement.stat()
    assert (old_stat.st_dev, old_stat.st_ino) != (replacement_stat.st_dev, replacement_stat.st_ino)
    os.replace(replacement, source)

    scan_library(conn, library)
    new_row = conn.execute(
        "SELECT sha256, fs_device_id, fs_object_id FROM files WHERE id=?", (file_row["id"],)
    ).fetchone()
    assert new_row["sha256"] == file_row["sha256"]
    assert (new_row["fs_device_id"], new_row["fs_object_id"]) != old_key

    reasons = [r["reason"] for r in conn.execute(
        "SELECT reason FROM file_storage_identity_history WHERE file_id=? ORDER BY id",
        (file_row["id"],),
    )]
    assert reasons == ["identity_established", "identity_changed"]
    event = conn.execute(
        "SELECT detail FROM integrity_events WHERE file_id=? AND event_type='filesystem_object_changed'",
        (file_row["id"],),
    ).fetchone()
    assert event is not None
    assert "Filesystem object identity changed" in event["detail"]
    conn.close()
