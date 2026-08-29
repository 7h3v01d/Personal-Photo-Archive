"""Phase 12.4.2 — execution-time physical content re-attestation regressions."""
from pathlib import Path

import pytest
from PIL import Image

from ppa.db import connect
from ppa.duplicate_lineage import validate_exact_copy_pair
from ppa.identity_merge import execute_identity_merge, plan_identity_merge
from ppa.identity_resolution import (
    execute_identity_recovery,
    execute_identity_split,
    plan_identity_recovery,
    plan_identity_split,
)
from ppa.physical_observation import PhysicalObservationError, observe_stable_image
from ppa.scanner import scan_library


def _img(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color=color).save(path)


def _rows(conn):
    return {r["filename"]: r for r in conn.execute("SELECT * FROM files ORDER BY filename")}


def _two_photos_converged(tmp_path: Path):
    library = tmp_path / "library"
    a, b = library / "a.jpg", library / "b.jpg"
    _img(a, "red"); _img(b, "blue")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    _img(b, "red")
    scan_library(conn, library)
    rows = _rows(conn)
    lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    assert rows["a.jpg"]["photo_id"] != rows["b.jpg"]["photo_id"]
    assert rows["a.jpg"]["sha256"] == rows["b.jpg"]["sha256"]
    return conn, lid, a, b, rows


def _one_photo_diverged(tmp_path: Path):
    library = tmp_path / "library"
    a, b = library / "a.jpg", library / "b.jpg"
    _img(a, "red"); _img(b, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    _img(b, "blue")
    scan_library(conn, library)
    rows = _rows(conn)
    lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    assert rows["a.jpg"]["photo_id"] == rows["b.jpg"]["photo_id"]
    assert rows["a.jpg"]["sha256"] != rows["b.jpg"]["sha256"]
    return conn, lid, a, b, rows


def test_exact_copy_rejects_external_change_without_verify(tmp_path: Path) -> None:
    library = tmp_path / "library"
    a, b = library / "a.jpg", library / "b.jpg"
    _img(a, "red"); _img(b, "red")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    rows = _rows(conn); lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    _img(b, "green")
    with pytest.raises(PhysicalObservationError, match="changed since identity review"):
        validate_exact_copy_pair(conn, library_id=lid, file_ids=(rows["a.jpg"]["id"], rows["b.jpg"]["id"]))


def test_merge_execution_rejects_external_change_without_verify(tmp_path: Path) -> None:
    conn, lid, _a, b, rows = _two_photos_converged(tmp_path)
    plan = plan_identity_merge(conn, library_id=lid, sha256=rows["a.jpg"]["sha256"],
                               survivor_photo_id=rows["a.jpg"]["photo_id"])
    _img(b, "green")
    with pytest.raises(PhysicalObservationError, match="changed since identity review"):
        execute_identity_merge(conn, plan)
    assert _rows(conn)["b.jpg"]["photo_id"] == rows["b.jpg"]["photo_id"]


def test_split_execution_rejects_external_change_without_verify(tmp_path: Path) -> None:
    conn, lid, _a, b, rows = _one_photo_diverged(tmp_path)
    plan = plan_identity_split(conn, library_id=lid, source_photo_id=rows["a.jpg"]["photo_id"],
                               file_ids=(rows["b.jpg"]["id"],))
    _img(b, "red")
    with pytest.raises(PhysicalObservationError, match="changed since identity review"):
        execute_identity_split(conn, plan)
    assert _rows(conn)["b.jpg"]["photo_id"] == rows["a.jpg"]["photo_id"]


def test_recovery_execution_rejects_external_change_without_verify(tmp_path: Path) -> None:
    conn, lid, a, _b, rows = _one_photo_diverged(tmp_path)
    split = execute_identity_split(
        conn,
        plan_identity_split(conn, library_id=lid, source_photo_id=rows["a.jpg"]["photo_id"],
                            file_ids=(rows["b.jpg"]["id"],)),
    )
    plan = plan_identity_recovery(conn, split.resolution_id)
    _img(a, "green")
    with pytest.raises(PhysicalObservationError, match="changed since identity review"):
        execute_identity_recovery(conn, plan)
    assert conn.execute("SELECT 1 FROM photos WHERE id=?", (split.new_photo_id,)).fetchone() is not None


def test_stable_observation_rejects_change_during_hash_cycle(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "one.jpg"
    _img(image, "red")
    import ppa.physical_observation as po
    real = po.sha256_file
    calls = {"n": 0}

    def racing(path: Path) -> str:
        calls["n"] += 1
        digest = real(path)
        if calls["n"] == 1:
            _img(path, "blue")
        return digest

    monkeypatch.setattr(po, "sha256_file", racing)
    with pytest.raises(PhysicalObservationError, match="changed while"):
        observe_stable_image(image)


def test_merge_rolls_back_if_file_changes_during_execution(tmp_path: Path, monkeypatch) -> None:
    conn, lid, _a, b, rows = _two_photos_converged(tmp_path)
    plan = plan_identity_merge(conn, library_id=lid, sha256=rows["a.jpg"]["sha256"],
                               survivor_photo_id=rows["a.jpg"]["photo_id"])
    import ppa.identity_merge as im
    real = im.require_expected_physical_bytes
    calls = {"n": 0}

    def changing_between_attestations(files, *, context):
        result = real(files, context=context)
        calls["n"] += 1
        if calls["n"] == 1:
            _img(b, "green")
        return result

    monkeypatch.setattr(im, "require_expected_physical_bytes", changing_between_attestations)
    with pytest.raises(PhysicalObservationError, match="changed since identity review"):
        execute_identity_merge(conn, plan)
    assert _rows(conn)["b.jpg"]["photo_id"] == rows["b.jpg"]["photo_id"]
    assert conn.execute("SELECT COUNT(*) FROM identity_merge_history").fetchone()[0] == 0
