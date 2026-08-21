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


def test_manage_libraries_dialog_lists_and_forgets(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.ui.libraries_dialog import LibrariesDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    dialog = LibrariesDialog(conn, None, None)
    assert dialog._table.rowCount() == 1
    assert dialog._table.item(0, 2).text() == "3"          # three photos present

    lib_id = catalogue.list_libraries(conn)[0].id
    catalogue.forget_library(conn, lib_id)
    dialog._reload()
    assert dialog._table.rowCount() == 0                   # forgotten from the list
    # Source photos are untouched on disk.
    assert (tmp_path / "library" / "IMG_0001.jpg").exists()
    conn.close()


def test_manage_libraries_scan_request_handoff(app, tmp_path: Path) -> None:
    from ppa.ui.libraries_dialog import LibrariesDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    dialog = LibrariesDialog(conn, None, None)
    # Selecting a row and 'rescan' records a request for the main window.
    dialog._table.selectRow(0)
    dialog._on_rescan()
    assert dialog.scan_request is not None
    conn.close()


def test_preview_dialog_loads_and_navigates(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.ui.models import PhotoGridModel
    from ppa.ui.preview_dialog import PreviewDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    model = PhotoGridModel()
    model.set_items(catalogue.grid_items(conn, catalogue.VIEW_ALL))
    assert model.rowCount() == 3

    dlg = PreviewDialog(conn, model, 0, None)
    app.processEvents()                                              # run deferred decode
    assert dlg._original is not None and not dlg._original.isNull()  # image loaded
    assert dlg._prev.isEnabled() is False                           # at first
    dlg._go_next(); app.processEvents()
    assert dlg._pos == 1 and "2 / 3" in dlg._caption.text()
    dlg._go_next(); dlg._go_next(); app.processEvents()             # clamps at end
    assert dlg._pos == 2 and dlg._next.isEnabled() is False
    assert len(dlg._cache) >= 1                                      # decoded images cached
    conn.close()


def test_preview_dialog_handles_missing_file(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.ui.models import PhotoGridModel
    from ppa.ui.preview_dialog import PreviewDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    model = PhotoGridModel()
    model.set_items(catalogue.grid_items(conn, catalogue.VIEW_ALL))
    # A file catalogued as present, then removed from disk before preview.
    (tmp_path / "library" / "IMG_0001.jpg").unlink()

    dlg = PreviewDialog(conn, model, 0, None)
    shown = False
    for pos in range(model.rowCount()):
        dlg._pos = pos; dlg._load(); app.processEvents()
        if dlg._original is None and "IMG_0001" in dlg._image.text():
            shown = True; break
    assert shown                                                     # placeholder, no crash
    conn.close()
