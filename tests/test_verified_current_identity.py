"""Phase 12.4.1 — real Verify-driven current-identity hardening regressions."""
from pathlib import Path

import pytest
from PIL import Image

from ppa.archive_health import build_archive_health
from ppa.competing_identity import investigate_competing_identity
from ppa.db import connect
from ppa.duplicate_lineage import add_lineage, build_duplicate_identity, validate_exact_copy_pair
from ppa.identity_health import build_identity_health
from ppa.identity_merge import execute_identity_merge, plan_identity_merge
from ppa.identity_resolution import (
    execute_identity_split,
    plan_identity_recovery,
    plan_identity_split,
    review_identity_resolution,
)
from ppa.integrity import verify_library
from ppa.scanner import scan_library


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color=color).save(path)


def _files_by_name(conn):
    return {r["filename"]: r for r in conn.execute("SELECT * FROM files ORDER BY filename")}


def _setup_two_distinct_then_converge(tmp_path: Path):
    """Return two logical Photos that become verified-current byte-identical."""
    library = tmp_path / "library"
    a = library / "a.jpg"
    b = library / "b.jpg"
    _img(a, "red")
    _img(b, "blue")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    before = _files_by_name(conn)
    assert before["a.jpg"]["photo_id"] != before["b.jpg"]["photo_id"]

    _img(b, "red")
    scan_library(conn, library)
    rows = _files_by_name(conn)
    assert rows["a.jpg"]["photo_id"] != rows["b.jpg"]["photo_id"]
    assert rows["a.jpg"]["sha256"] == rows["b.jpg"]["sha256"]
    library_id = conn.execute("SELECT id FROM libraries").fetchone()[0]
    return conn, library, a, b, library_id, rows


def _setup_one_photo_then_diverge(tmp_path: Path):
    """Return one logical Photo with two verified-current SHA cohorts."""
    library = tmp_path / "library"
    a = library / "a.jpg"
    b = library / "b.jpg"
    _img(a, "red")
    _img(b, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    rows = _files_by_name(conn)
    assert rows["a.jpg"]["photo_id"] == rows["b.jpg"]["photo_id"]

    _img(b, "blue")
    scan_library(conn, library)
    rows = _files_by_name(conn)
    assert rows["a.jpg"]["photo_id"] == rows["b.jpg"]["photo_id"]
    assert rows["a.jpg"]["sha256"] != rows["b.jpg"]["sha256"]
    library_id = conn.execute("SELECT id FROM libraries").fetchone()[0]
    return conn, library, a, b, library_id, rows


def test_verified_mismatch_not_reported_or_validated_as_exact_duplicate(tmp_path: Path) -> None:
    library = tmp_path / "library"
    a = library / "a.jpg"
    b = library / "b.jpg"
    _img(a, "red")
    _img(b, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    rows = _files_by_name(conn)
    library_id = conn.execute("SELECT id FROM libraries").fetchone()[0]

    _img(b, "blue")
    report = verify_library(conn)
    assert report.mismatches == 1
    mismatch = _files_by_name(conn)["b.jpg"]
    assert mismatch["health_status"] == "hash_mismatch"
    # Expected SHA remains equal by design; it is no longer current-byte proof.
    assert mismatch["sha256"] == rows["a.jpg"]["sha256"]

    view = build_duplicate_identity(conn, library_id=library_id)
    assert view.sets == ()
    with pytest.raises(ValueError, match="not proven current exact copies"):
        validate_exact_copy_pair(conn, library_id=library_id,
                                 file_ids=(rows["a.jpg"]["id"], mismatch["id"]))


def test_competing_identity_and_merge_reject_verified_mismatch(tmp_path: Path) -> None:
    conn, _library, _a, b, library_id, rows = _setup_two_distinct_then_converge(tmp_path)
    shared_sha = rows["a.jpg"]["sha256"]
    assert investigate_competing_identity(conn, library_id=library_id, sha256=shared_sha).merge_consideration.eligible

    _img(b, "green")
    assert verify_library(conn).mismatches == 1
    with pytest.raises(ValueError, match="not assigned to multiple"):
        investigate_competing_identity(conn, library_id=library_id, sha256=shared_sha)
    with pytest.raises(ValueError, match="not eligible|not assigned"):
        plan_identity_merge(conn, library_id=library_id, sha256=shared_sha,
                            survivor_photo_id=rows["a.jpg"]["photo_id"])


def test_identity_merge_execute_stales_when_health_changes_after_plan(tmp_path: Path) -> None:
    conn, _library, _a, b, library_id, rows = _setup_two_distinct_then_converge(tmp_path)
    shared_sha = rows["a.jpg"]["sha256"]
    plan = plan_identity_merge(conn, library_id=library_id, sha256=shared_sha,
                               survivor_photo_id=rows["a.jpg"]["photo_id"])
    _img(b, "green")
    verify_library(conn)
    with pytest.raises(ValueError, match="stale"):
        execute_identity_merge(conn, plan)
    assert _files_by_name(conn)["b.jpg"]["photo_id"] == rows["b.jpg"]["photo_id"]


def test_identity_split_rejects_verified_mismatch(tmp_path: Path) -> None:
    conn, _library, _a, b, library_id, rows = _setup_one_photo_then_diverge(tmp_path)
    source_photo = rows["a.jpg"]["photo_id"]
    b_id = rows["b.jpg"]["id"]

    # Current bytes converge back to red, but Verify preserves expected blue and
    # marks the current byte identity unknown until mismatch resolution.
    _img(b, "red")
    assert verify_library(conn).mismatches == 1
    with pytest.raises(ValueError, match="verified current content"):
        plan_identity_split(conn, library_id=library_id, source_photo_id=source_photo,
                            file_ids=(b_id,))


def test_identity_split_execute_stales_when_health_changes_after_plan(tmp_path: Path) -> None:
    conn, _library, _a, b, library_id, rows = _setup_one_photo_then_diverge(tmp_path)
    source_photo = rows["a.jpg"]["photo_id"]
    plan = plan_identity_split(conn, library_id=library_id, source_photo_id=source_photo,
                               file_ids=(rows["b.jpg"]["id"],))
    _img(b, "green")
    verify_library(conn)
    with pytest.raises(ValueError, match="stale"):
        execute_identity_split(conn, plan)
    assert _files_by_name(conn)["b.jpg"]["photo_id"] == source_photo


def test_identity_recovery_rejects_unhealthy_file(tmp_path: Path) -> None:
    conn, _library, _a, b, library_id, rows = _setup_one_photo_then_diverge(tmp_path)
    split = execute_identity_split(
        conn,
        plan_identity_split(conn, library_id=library_id,
                            source_photo_id=rows["a.jpg"]["photo_id"],
                            file_ids=(rows["b.jpg"]["id"],)),
    )
    _img(b, "green")
    verify_library(conn)
    review = review_identity_resolution(conn, split.resolution_id)
    assert review.recovery_eligible is False
    assert "verified current content" in review.recovery_reason
    with pytest.raises(ValueError, match="cannot be recombined"):
        plan_identity_recovery(conn, split.resolution_id)


def test_lineage_equal_hash_guard_ignores_unverified_current_hash(tmp_path: Path) -> None:
    conn, _library, _a, b, _library_id, rows = _setup_two_distinct_then_converge(tmp_path)
    _img(b, "green")
    verify_library(conn)
    # The two expected hashes are still equal, but only one side has verified
    # current bytes. The old raw-SHA prohibition must not claim byte identity.
    rel = add_lineage(conn, parent_photo_id=rows["a.jpg"]["photo_id"],
                      child_photo_id=rows["b.jpg"]["photo_id"],
                      relation_type="edited_variant")
    assert rel.parent_photo_id == rows["a.jpg"]["photo_id"]


def test_identity_health_routes_mismatch_to_integrity_resolution_not_current_hash_advice(tmp_path: Path) -> None:
    library = tmp_path / "library"
    a = library / "a.jpg"
    b = library / "b.jpg"
    _img(a, "red")
    _img(b, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    _img(b, "blue")
    verify_library(conn)
    library_id = conn.execute("SELECT id FROM libraries").fetchone()[0]

    view = build_identity_health(conn, library_id=library_id)
    assert any(i.kind == "integrity_resolution_required" for i in view.items)
    assert not any(i.kind in {"competing_identity", "identity_divergence"} for i in view.items)


def test_scan_observes_restored_expected_bytes_but_verify_owns_health_reconciliation(tmp_path: Path) -> None:
    library = tmp_path / "library"
    image = library / "one.jpg"
    _img(image, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    _img(image, "blue")
    verify_library(conn)
    assert conn.execute("SELECT health_status FROM files").fetchone()[0] == "hash_mismatch"

    _img(image, "red")
    scan_library(conn, library)
    assert conn.execute("SELECT health_status FROM files").fetchone()[0] == "hash_mismatch"
    assert conn.execute(
        "SELECT 1 FROM integrity_events WHERE event_type='expected_content_reobserved_pending_verify'"
    ).fetchone() is not None

    verify_library(conn)
    assert conn.execute("SELECT health_status FROM files").fetchone()[0] == "ok"



def test_archive_health_does_not_call_stale_expected_hash_current_content(tmp_path: Path) -> None:
    library = tmp_path / "library"
    a = library / "a.jpg"
    b = library / "b.jpg"
    _img(a, "red")
    _img(b, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    library_id = conn.execute("SELECT id FROM libraries").fetchone()[0]

    _img(b, "blue")
    assert verify_library(conn).mismatches == 1
    health = build_archive_health(conn, library_id=library_id)
    assert health.schema == "ppa-archive-health/4"
    assert health.multiple_exact_present_count == 0
    assert health.divergent_count == 0
    assert health.unhealthy_present_count == 1
    assert health.unknown_hash_count == 1


def test_restored_expected_bytes_do_not_clear_prior_mismatch_until_verify(tmp_path: Path) -> None:
    library = tmp_path / "library"
    image = library / "one.jpg"
    _img(image, "red")
    expected_bytes = image.read_bytes()
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)

    _img(image, "blue")
    assert verify_library(conn).mismatches == 1
    assert conn.execute("SELECT health_status FROM files").fetchone()[0] == "hash_mismatch"

    image.unlink()
    scan_library(conn, library)
    row = conn.execute("SELECT presence_status,health_status FROM files").fetchone()
    assert row["presence_status"] == "missing"
    assert row["health_status"] == "hash_mismatch"

    image.write_bytes(expected_bytes)
    scan_library(conn, library)
    row = conn.execute("SELECT presence_status,health_status FROM files").fetchone()
    assert row["presence_status"] == "present"
    assert row["health_status"] == "hash_mismatch"
    assert conn.execute(
        "SELECT 1 FROM integrity_events WHERE event_type='expected_content_reobserved_pending_verify'"
    ).fetchone() is not None

    verify_library(conn)
    assert conn.execute("SELECT health_status FROM files").fetchone()[0] == "ok"
