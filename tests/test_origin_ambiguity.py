"""Phase 12.2 — fail-closed restoration/origin reconciliation."""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from ppa.archive_health import build_archive_health, build_archive_health_browse
from ppa.db import connect
from ppa.scanner import scan_library


def _img(path: Path, color=(70, 100, 130)) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (43, 31), color).save(path)
    return path.read_bytes()


def _two_exact_copies(library: Path) -> tuple[Path, Path, bytes]:
    a = library / "a.jpg"
    b = library / "b.jpg"
    data = _img(a)
    b.write_bytes(data)
    return a, b, data


def test_multiple_missing_same_hash_reappearance_preserves_ambiguous_origin(tmp_path: Path) -> None:
    library = tmp_path / "library"
    a, b, data = _two_exact_copies(library)
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    originals = conn.execute(
        "SELECT id,photo_id,filename FROM files ORDER BY filename"
    ).fetchall()
    assert len(originals) == 2
    original_ids = [r["id"] for r in originals]
    original_photo = originals[0]["photo_id"]
    assert {r["photo_id"] for r in originals} == {original_photo}

    a.unlink(); b.unlink()
    missing = scan_library(conn, library)
    assert missing.missing_files == 2

    recovered = library / "recovered" / "found.jpg"
    recovered.parent.mkdir(parents=True)
    recovered.write_bytes(data)

    # Adversarial premise: even if stale catalogue identity happens to match
    # this new object's current stat token, PPA must not use it as proof of
    # historical origin because filesystem object ids can be reused over time.
    st = recovered.stat()
    conn.execute(
        "UPDATE files SET fs_device_id=?, fs_object_id=? WHERE id=?",
        (str(st.st_dev), str(st.st_ino), original_ids[0]),
    )
    conn.commit()

    report = scan_library(conn, library)
    assert report.ambiguous_origin_files == 1
    assert report.restored_files == 0
    assert report.moved_files == 0
    assert report.new_files == 0

    rows = conn.execute(
        "SELECT id,photo_id,path,presence_status FROM files ORDER BY first_seen_at,id"
    ).fetchall()
    assert len(rows) == 3
    old_rows = [r for r in rows if r["id"] in original_ids]
    new_row = next(r for r in rows if r["id"] not in original_ids)
    assert {r["presence_status"] for r in old_rows} == {"missing"}
    assert new_row["presence_status"] == "present"
    assert new_row["path"] == str(recovered.resolve())
    # Logical Photo identity is still certain because every candidate belonged
    # to the same Photo; only physical File origin is ambiguous.
    assert new_row["photo_id"] == original_photo

    ambiguity = conn.execute("SELECT * FROM file_origin_ambiguities").fetchone()
    assert ambiguity is not None
    assert ambiguity["observed_file_id"] == new_row["id"]
    assert ambiguity["ambiguity_kind"] == "ambiguous_restoration"
    assert json.loads(ambiguity["candidate_file_ids_json"]) == sorted(original_ids)
    assert json.loads(ambiguity["candidate_photo_ids_json"]) == [original_photo]

    event = conn.execute(
        "SELECT detail FROM integrity_events WHERE file_id=? AND event_type='origin_ambiguous'",
        (new_row["id"],),
    ).fetchone()
    assert event is not None
    assert "No candidate File was selected" in event["detail"]

    health = build_archive_health(conn, library_id=1)
    assert health.schema == "ppa-archive-health/4"
    assert health.ambiguous_origin_count == 1
    assert original_photo in health.attention_photo_ids
    assert build_archive_health_browse(conn, health, "ambiguous_origin").total_members == 1
    conn.close()


def test_same_path_restore_remains_unambiguous_with_identical_missing_peer(tmp_path: Path) -> None:
    library = tmp_path / "library"
    a, b, data = _two_exact_copies(library)
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id,filename FROM files")}

    a.unlink(); b.unlink()
    scan_library(conn, library)
    a.write_bytes(data)  # same canonical within-library path as historical a.jpg

    report = scan_library(conn, library)
    assert report.restored_files == 1
    assert report.ambiguous_origin_files == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 2
    a_row = conn.execute("SELECT presence_status,path FROM files WHERE id=?", (ids["a.jpg"],)).fetchone()
    b_row = conn.execute("SELECT presence_status FROM files WHERE id=?", (ids["b.jpg"],)).fetchone()
    assert a_row["presence_status"] == "present"
    assert a_row["path"] == str(a.resolve())
    assert b_row["presence_status"] == "missing"
    assert conn.execute("SELECT COUNT(*) AS n FROM file_origin_ambiguities").fetchone()["n"] == 0
    conn.close()


def test_ambiguous_candidates_across_photos_do_not_cross_identity_boundary(tmp_path: Path) -> None:
    library = tmp_path / "library"
    a, b, data = _two_exact_copies(library)
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    rows = conn.execute("SELECT id,photo_id,filename FROM files ORDER BY filename").fetchall()
    first_photo = rows[0]["photo_id"]

    # Model a legitimate prior human identity split: same bytes now belong to
    # two distinct logical Photos. The scanner must not pick either Photo merely
    # because the bytes match.
    second_photo = "phase12-2-second-photo"
    conn.execute("INSERT INTO photos(id) VALUES (?)", (second_photo,))
    conn.execute("UPDATE files SET photo_id=? WHERE id=?", (second_photo, rows[1]["id"]))
    conn.commit()

    a.unlink(); b.unlink()
    scan_library(conn, library)
    recovered = library / "recovered.jpg"
    recovered.write_bytes(data)

    report = scan_library(conn, library)
    assert report.ambiguous_origin_files == 1
    observed = conn.execute(
        "SELECT f.id,f.photo_id FROM files f WHERE f.presence_status='present'"
    ).fetchone()
    assert observed["photo_id"] not in {first_photo, second_photo}

    ambiguity = conn.execute("SELECT candidate_photo_ids_json FROM file_origin_ambiguities").fetchone()
    assert json.loads(ambiguity["candidate_photo_ids_json"]) == sorted([first_photo, second_photo])
    assert conn.execute("SELECT 1 FROM photos WHERE id=?", (observed["photo_id"],)).fetchone() is not None
    conn.close()
