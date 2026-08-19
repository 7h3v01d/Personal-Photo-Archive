"""Offscreen GUI smoke tests.

These construct the real window under the 'offscreen' Qt platform and drive
the model directly. They verify the UI wires up and reflects the catalogue;
they are not pixel tests. Skipped cleanly if Qt can't start headless.
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ppa.config import Config  # noqa: E402
from ppa.db import connect  # noqa: E402
from ppa.scanner import scan_library  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _config_with_library(tmp_path: Path) -> Config:
    library = tmp_path / "library"
    library.mkdir(parents=True)
    for i, color in enumerate(["red", "green", "blue"], start=1):
        Image.new("RGB", (60, 40), color).save(library / f"IMG_{i:04d}.jpg")

    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    conn.close()

    cfg_path = tmp_path / "config.toml"
    # Use POSIX-style (forward-slash) paths: backslashes in a double-quoted TOML
    # string are escape sequences (a Windows path like C:\Users\... breaks on the
    # \U). Forward slashes are valid TOML and are accepted by pathlib/SQLite on
    # Windows too.
    db_path = (tmp_path / "catalogue.sqlite3").as_posix()
    log_path = (tmp_path / "ppa.log").as_posix()
    lib_path = library.as_posix()
    cfg_path.write_text(
        f"""
[database]
path = "{db_path}"
[logging]
level = "INFO"
path = "{log_path}"
[library]
directories = ["{lib_path}"]
""",
        encoding="utf-8",
    )
    return Config.load(cfg_path)


def test_window_constructs_and_populates(app, tmp_path: Path) -> None:
    from ppa.ui.main_window import MainWindow

    config = _config_with_library(tmp_path)
    win = MainWindow(config)
    try:
        assert win._model.rowCount() == 3  # three active photos in All view
        # Switch to a different named view and confirm the grid updates.
        win._nav.setCurrentRow(3)  # Missing
        assert win._model.rowCount() == 0
        win._nav.setCurrentRow(0)  # back to All
        assert win._model.rowCount() == 3
    finally:
        win._registry.shutdown()
        win.close()


def test_selection_populates_inspector(app, tmp_path: Path) -> None:
    from ppa.ui.main_window import MainWindow
    from ppa.catalogue import file_detail

    config = _config_with_library(tmp_path)
    win = MainWindow(config)
    try:
        idx = win._model.index(0)
        item = win._model.item_at(idx)
        assert item is not None
        detail = file_detail(win._conn, item.file_id)
        assert detail is not None and detail.sha256
    finally:
        win._registry.shutdown()
        win.close()
