"""Phase 13.0.1 archive-safe user-directed output regressions."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from ppa.config import Config
from ppa.db import connect
from ppa.safe_export import ArchiveOutputSafetyError, safe_export_text
from ppa.scanner import scan_library


def _case(tmp_path: Path):
    library = tmp_path / "library"
    source = library / "source.jpg"
    library.mkdir()
    Image.new("RGB", (40, 30), "red").save(source)
    db = tmp_path / "data" / "catalogue.sqlite3"
    conn = connect(db)
    scan_library(conn, library)
    config = Config(
        db_path=db,
        log_level="INFO",
        log_path=tmp_path / "data" / "logs" / "ppa.log",
        library_directories=[library],
    )
    return conn, config, library, source


def test_export_inside_registered_library_is_rejected_without_source_change(tmp_path: Path) -> None:
    conn, config, library, source = _case(tmp_path)
    before = source.read_bytes()
    with pytest.raises(ArchiveOutputSafetyError, match="source Library"):
        safe_export_text(source, "NOT AN IMAGE", conn=conn, config=config)
    assert source.read_bytes() == before
    with pytest.raises(ArchiveOutputSafetyError, match="source Library"):
        safe_export_text(library / "report.json", "{}", conn=conn, config=config)


def test_export_rejects_hardlink_alias_to_catalogued_source(tmp_path: Path) -> None:
    conn, config, _library, source = _case(tmp_path)
    alias = tmp_path / "outside-source-alias.jpg"
    try:
        os.link(source, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hard links unavailable in this environment")
    before = source.read_bytes()
    with pytest.raises(ArchiveOutputSafetyError, match="filesystem object"):
        safe_export_text(alias, "NOT AN IMAGE", conn=conn, config=config)
    assert source.read_bytes() == before
    assert alias.read_bytes() == before


def test_export_rejects_symlink_alias_into_library(tmp_path: Path) -> None:
    conn, config, _library, source = _case(tmp_path)
    alias = tmp_path / "outside-source-link.json"
    try:
        alias.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable in this environment")
    before = source.read_bytes()
    with pytest.raises(ArchiveOutputSafetyError, match="source Library|source File"):
        safe_export_text(alias, "{}", conn=conn, config=config)
    assert source.read_bytes() == before


def test_export_rejects_operational_paths_and_allows_normal_external_destination(tmp_path: Path) -> None:
    conn, config, _library, _source = _case(tmp_path)
    with pytest.raises(ArchiveOutputSafetyError, match="operational"):
        safe_export_text(config.db_path, "bad", conn=conn, config=config)
    with pytest.raises(ArchiveOutputSafetyError, match="operational"):
        safe_export_text(config.db_path.parent / "thumbnails" / "bad.json", "bad", conn=conn, config=config)
    out = safe_export_text(tmp_path / "exports" / "report.json", "{\"ok\": true}\n", conn=conn, config=config)
    assert out.read_text(encoding="utf-8") == '{"ok": true}\n'
