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

from PySide6.QtWidgets import QApplication, QToolBar  # noqa: E402

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


def _reset_run_catalogue(tmp_path):
    from PIL import ExifTags
    from ppa import anchors, metadata
    from ppa.reconstruct_catalogue import store_reconstructions
    lib = tmp_path / "library2"
    for i in range(5):
        p = lib / f"IMG_{201+i:04d}.jpg"; p.parent.mkdir(parents=True, exist_ok=True)
        im = Image.new("RGB", (48, 36), "red")
        ex = im.getexif(); ex[0x010F] = "Canon"; ex[0x0110] = "5D"
        s = ex.get_ifd(ExifTags.IFD.Exif); s[0x9003] = f"2001:01:01 00:{i*5:02d}:00"
        s[0xA431] = "SN-1"
        im.save(p, format="JPEG", exif=ex)
    conn = connect(tmp_path / "db2.sqlite3")
    from ppa.scanner import scan_library
    scan_library(conn, lib); metadata.extract_stale(conn)
    fid = conn.execute("SELECT id FROM files WHERE filename='IMG_0203.jpg'").fetchone()["id"]
    anchors.add_anchor(conn, "file", fid, "exact", "2004-12-25")
    store_reconstructions(conn)
    return conn, fid


def test_preview_caption_shows_recorded_and_reconstructed(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.ui.models import PhotoGridModel
    from ppa.ui.preview_dialog import PreviewDialog
    conn, fid = _reset_run_catalogue(tmp_path)
    model = PhotoGridModel(); model.set_items(catalogue.grid_items(conn, catalogue.VIEW_ALL))
    start = next(i for i, it in enumerate(model._items) if it.file_id == fid)
    dlg = PreviewDialog(conn, model, start, None); app.processEvents()
    cap = dlg._caption.text()
    assert "Recorded 2001-01-01" in cap and "questionable" in cap
    assert "2004-12-25" in cap                    # the reconstructed date is shown
    conn.close()


def test_preview_confirm_and_reopen_flow(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.reconstruct_catalogue import list_reconstructions
    from ppa.ui.models import PhotoGridModel
    from ppa.ui.preview_dialog import PreviewDialog
    conn, fid = _reset_run_catalogue(tmp_path)
    model = PhotoGridModel(); model.set_items(catalogue.grid_items(conn, catalogue.VIEW_ALL))
    start = next(i for i, it in enumerate(model._items) if it.file_id == fid)
    dlg = PreviewDialog(conn, model, start, None); app.processEvents()
    assert dlg._confirm_btn.isVisibleTo(dlg)          # proposed -> confirm offered
    dlg._decide("confirm"); app.processEvents()
    assert list_reconstructions(conn, status="confirmed")   # confirmed in DB
    assert dlg._reopen_btn.isVisibleTo(dlg)           # decided -> reopen offered
    dlg._reopen(); app.processEvents()
    assert list_reconstructions(conn, status="proposed")    # back to proposed
    conn.close()


def test_preview_shows_evidence_stale_confirmed(app, tmp_path: Path) -> None:
    from ppa import anchors, catalogue
    from ppa.reconstruct_catalogue import confirm_reconstruction, store_reconstructions
    from ppa.ui.models import PhotoGridModel
    from ppa.ui.preview_dialog import PreviewDialog
    conn, fid = _reset_run_catalogue(tmp_path)
    confirm_reconstruction(conn, fid)                          # confirm 2004
    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")
    store_reconstructions(conn)                                # evidence moved on

    model = PhotoGridModel(); model.set_items(catalogue.grid_items(conn, catalogue.VIEW_ALL))
    start = next(i for i, it in enumerate(model._items) if it.file_id == fid)
    dlg = PreviewDialog(conn, model, start, None); app.processEvents()
    cap = dlg._caption.text()
    assert "STALE" in cap and "evidence changed" in cap        # not shown as fresh
    assert "STALE" in dlg._review_status.text()
    assert dlg._reopen_btn.text() == "Reopen && refresh"
    conn.close()


def test_preview_stale_proposal_offers_refresh_not_confirm(app, tmp_path: Path) -> None:
    from ppa import anchors, catalogue
    from ppa.ui.models import PhotoGridModel
    from ppa.ui.preview_dialog import PreviewDialog
    conn, fid = _reset_run_catalogue(tmp_path)                 # proposed 2004
    anchors.add_anchor(conn, "file", fid, "exact", "2005-12-25")  # evidence changed, no re-run

    model = PhotoGridModel(); model.set_items(catalogue.grid_items(conn, catalogue.VIEW_ALL))
    start = next(i for i, it in enumerate(model._items) if it.file_id == fid)
    dlg = PreviewDialog(conn, model, start, None); app.processEvents()
    assert not dlg._confirm_btn.isVisibleTo(dlg)               # stale -> no direct confirm
    assert dlg._refresh_btn.isVisibleTo(dlg)                   # refresh offered
    dlg._refresh(); app.processEvents()
    assert dlg._confirm_btn.isVisibleTo(dlg)                   # refreshed -> confirmable
    assert "2005-12-25" in dlg._caption.text()                # now the current proposal
    conn.close()


def test_phase8_needs_attention_includes_hidden_event_members(app, tmp_path: Path) -> None:
    """Regression for 8.14: incomplete hidden-member Events must be actionable."""
    from ppa import catalogue
    from ppa.event_health import build_event_health_view
    from ppa.event_home import build_event_home
    from ppa.event_search import build_event_search_index
    from ppa.events import create_event_from_cluster, update_event_context
    from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
    from ppa.timeline_clusters import DayCount, TimelineCluster
    from ppa.ui.event_home_dialog import EventHomeDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    rows = conn.execute("SELECT id,filename FROM files ORDER BY filename").fetchall()
    ids = tuple(r["id"] for r in rows)
    cluster = TimelineCluster(
        "ui-hidden", "day_burst", "x", "2004-12-25", "2004-12-25",
        len(ids), ids, (), (DayCount("2004-12-25", len(ids)),), "test")
    event = create_event_from_cluster(conn, library_id=lib_id, cluster=cluster, name="Christmas")
    update_event_context(conn, event.id, story_text="Family story")
    one_cluster = TimelineCluster(
        "ui-complete", "day_burst", "x", "2004-12-25", "2004-12-25",
        1, (ids[0],), (), (DayCount("2004-12-25", 1),), "test")
    complete_event = create_event_from_cluster(conn, library_id=lib_id, cluster=one_cluster, name="Lunch")
    update_event_context(conn, complete_event.id, story_text="Lunch story")

    # Deliberately project only one member. The other durable Christmas Event members are
    # outside the current Timeline scope and must therefore trigger Attention.
    item = TimelineItem(ids[0], rows[0]["filename"], "placed", "reconciled", "2004-12-25", None,
                        "PROBABLY_VALID", None, None, None, False, False, "test")
    scope = type("Scope", (), {"library_id": lib_id})()
    lanes = {
        "placed": TimelineBucket("placed", 1, (ids[0],)),
        "range": TimelineBucket("range", 0, ()),
        "tentative": TimelineBucket("tentative", 0, ()),
        "unplaced": TimelineBucket("unplaced", 0, ()),
    }
    view = TimelineView("ppa-timeline/1", "x", True, scope, (item,), lanes, ())
    home = build_event_home(conn, view)
    search = build_event_search_index(conn, home)
    health = build_event_health_view(conn, view)
    assert health.event(event.id).hidden_members == 2
    assert health.event(event.id).needs_attention

    dlg = EventHomeDialog(conn, view, home, search, health, None, cache_dir=tmp_path / "thumbs")
    try:
        attention_index = dlg._activity_filter.findData("attention")
        dlg._activity_filter.setCurrentIndex(attention_index)
        app.processEvents()
        assert [c.event_id for c in dlg._visible] == [event.id]
        complete_index = dlg._activity_filter.findData("complete")
        dlg._activity_filter.setCurrentIndex(complete_index)
        app.processEvents()
        assert [c.event_id for c in dlg._visible] == [complete_event.id]
        dlg._search.setText("Lunch")
        app.processEvents()
        assert [c.event_id for c in dlg._visible] == [complete_event.id]
    finally:
        dlg._registry.shutdown()
        dlg.close()
        conn.close()


def test_worker_registry_delivers_gui_receiver_on_main_qt_thread(app) -> None:
    """Cross-thread regression: GUI receiver must execute on QApplication thread."""
    from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Signal, Slot, Qt
    from ppa.ui.workers import WorkerRegistry

    class ProbeWorker(QObject):
        finished = Signal(object)

        @Slot()
        def run(self):
            self.finished.emit(QThread.currentThread())

    class Receiver(QObject):
        def __init__(self, loop):
            super().__init__()
            self.loop = loop
            self.sender_thread = None
            self.receiver_thread = None

        @Slot(object)
        def receive(self, sender_thread):
            self.sender_thread = sender_thread
            self.receiver_thread = QThread.currentThread()
            self.loop.quit()

    registry = WorkerRegistry()
    loop = QEventLoop()
    worker = ProbeWorker()
    receiver = Receiver(loop)
    worker.finished.connect(receiver.receive, Qt.ConnectionType.QueuedConnection)
    registry.start(worker)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    try:
        assert receiver.sender_thread is not None
        assert receiver.sender_thread is not app.thread()
        assert receiver.receiver_thread is app.thread()
    finally:
        registry.shutdown()


def test_phase8_timeline_and_story_dialogs_construct(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.events import create_event_from_cluster, update_event_context
    from ppa.timeline import build_timeline
    from ppa.timeline_clusters import DayCount, TimelineCluster
    from ppa.ui.event_story_dialog import EventStoryDialog
    from ppa.ui.timeline_dialog import TimelineDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    view = build_timeline(conn, library_id=lib_id)
    ids = tuple(i.file_id for i in view.items)
    cluster = TimelineCluster(
        "ui-story", "day_burst", "x", "2004-12-25", "2004-12-25",
        len(ids), ids, (), (DayCount("2004-12-25", len(ids)),), "test")
    event = create_event_from_cluster(conn, library_id=lib_id, cluster=cluster, name="Story Event")
    update_event_context(conn, event.id, story_text="A human-authored story")

    timeline = TimelineDialog(conn, view, None, cache_dir=tmp_path / "timeline-thumbs")
    story = EventStoryDialog(conn, view, event.id, None, cache_dir=tmp_path / "story-thumbs")
    try:
        app.processEvents()
        assert timeline.windowTitle() == "Chronology Timeline"
        assert story.windowTitle() == "Story Event"
        assert story._story.event.id == event.id
    finally:
        timeline._registry.shutdown(); timeline.close()
        story._registry.shutdown(); story.close()
        conn.close()


def test_legacy_workers_close_sqlite_connection_on_failure(app, monkeypatch, tmp_path: Path) -> None:
    """Scan/Verify/Metadata must close their worker-owned DB on exceptions."""
    from ppa.ui import workers

    class FakeConnection:
        def __init__(self):
            self.closed = False
        def close(self):
            self.closed = True

    cases = [
        (workers.ScanWorker(tmp_path / "x.sqlite", tmp_path), "scan_library"),
        (workers.VerifyWorker(tmp_path / "x.sqlite"), "verify_library"),
        (workers.MetadataWorker(tmp_path / "x.sqlite"), "extract_stale"),
    ]
    for worker, operation_name in cases:
        fake = FakeConnection()
        monkeypatch.setattr(workers, "connect", lambda _path, f=fake: f)
        def boom(*_args, **_kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr(workers, operation_name, boom)
        worker.run()
        assert fake.closed, operation_name


def test_phase9_organization_dialog_constructs_and_bulk_curates(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.organization import create_album, create_tag, get_album, get_tag
    from ppa.ui.organization_dialog import OrganizationDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    items = catalogue.grid_items(conn)
    photo_ids = tuple(dict.fromkeys(i.photo_id for i in items[:2]))
    album = create_album(conn, library_id=lib_id, name="Family")
    tag = create_tag(conn, library_id=lib_id, name="Favourite")
    dialog = OrganizationDialog(conn, lib_id, photo_ids, None)
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Albums & Tags"
        assert dialog._album_list.count() == 1
        assert dialog._tag_list.count() == 1
        dialog._album_list.setCurrentRow(0); dialog._album_add()
        dialog._tag_list.setCurrentRow(0); dialog._tag_add()
        assert set(get_album(conn, album.id).photo_ids) == set(photo_ids)
        assert set(get_tag(conn, tag.id).photo_ids) == set(photo_ids)
    finally:
        dialog.close(); conn.close()


def test_phase9_organization_browser_constructs_filters_and_keeps_logical_photo_unique(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.organization import create_album, add_photo_to_album, get_album
    from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    items = catalogue.grid_items(conn)
    album = create_album(conn, library_id=lib_id, name="Browse Test")
    for pid in tuple(dict.fromkeys(i.photo_id for i in items[:3])):
        add_photo_to_album(conn, album.id, pid)
    album = get_album(conn, album.id)
    dialog = OrganizationBrowseDialog(conn, "album", album.id, None,
                                      cache_dir=tmp_path / "org-thumbs")
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Album: Browse Test"
        assert dialog._model.rowCount() == len(album.photo_ids)
        if dialog._model.rowCount():
            filename = dialog._model._items[0].filename
            dialog._search.setText(filename)
            app.processEvents()
            assert dialog._model.rowCount() >= 1
    finally:
        dialog.close(); conn.close()


def test_phase9_album_presentation_dialog_constructs_and_persists(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.organization import create_album, add_photo_to_album, get_album_presentation
    from ppa.ui.album_presentation_dialog import AlbumPresentationDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    items = catalogue.grid_items(conn)
    photo_ids = tuple(dict.fromkeys(i.photo_id for i in items[:2]))
    album = create_album(conn, library_id=lib_id, name="Presentation Test")
    for pid in photo_ids:
        add_photo_to_album(conn, album.id, pid)
    dialog = AlbumPresentationDialog(conn, album.id, None)
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Album presentation"
        assert dialog._list.count() == len(photo_ids)
        if photo_ids:
            dialog._list.setCurrentRow(0); dialog._cover()
            assert get_album_presentation(conn, album.id).cover_photo_id is not None
        if len(photo_ids) > 1:
            dialog._list.setCurrentRow(1); dialog._move(-1); dialog._save()
            assert get_album_presentation(conn, album.id).order_photo_ids is not None
    finally:
        dialog.close(); conn.close()


def test_phase9_album_home_constructs_filters_and_opens_card(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.album_home import build_album_home
    from ppa.organization import create_album, add_photo_to_album
    from ppa.ui.album_home_dialog import AlbumHomeDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    items = catalogue.grid_items(conn)
    album = create_album(conn, library_id=lib_id, name="Family Trips", description="Beach memories")
    for pid in tuple(dict.fromkeys(i.photo_id for i in items[:2])):
        add_photo_to_album(conn, album.id, pid)
    home = build_album_home(conn, library_id=lib_id)
    dialog = AlbumHomeDialog(conn, home, None, cache_dir=tmp_path / "album-home-thumbs")
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Albums"
        assert dialog._list.count() == 1
        dialog._search.setText("beach")
        app.processEvents()
        assert dialog._list.count() == 1
        dialog._search.setText("does-not-exist")
        app.processEvents()
        assert dialog._list.count() == 0
    finally:
        dialog.close(); conn.close()


def test_phase9_tag_home_constructs_and_intersects(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.organization import create_tag, tag_photo
    from ppa.tag_home import build_tag_home, build_tag_intersection_view
    from ppa.ui.tag_home_dialog import TagHomeDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    pids = tuple(dict.fromkeys(i.photo_id for i in catalogue.grid_items(conn)))
    a=create_tag(conn,library_id=lib_id,name='Family'); b=create_tag(conn,library_id=lib_id,name='Beach')
    for pid in pids[:2]: tag_photo(conn,a.id,pid)
    for pid in pids[1:3]: tag_photo(conn,b.id,pid)
    dialog=TagHomeDialog(conn,build_tag_home(conn,library_id=lib_id),None,cache_dir=tmp_path/'tag-home-thumbs')
    try:
        app.processEvents(); assert dialog.windowTitle()=='Tags'; assert dialog._list.count()==2
        for i in range(dialog._list.count()): dialog._list.item(i).setSelected(True)
        app.processEvents(); assert dialog._intersection.isEnabled()
        view=build_tag_intersection_view(conn,library_id=lib_id,tag_ids=dialog._selected_ids())
        assert view.total_members==1
    finally:
        dialog.close(); conn.close()


def test_phase9_unified_organization_discovery_constructs_and_selects(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.album_home import build_album_home
    from ppa.tag_home import build_tag_home
    from ppa.organization import create_album, add_photo_to_album, create_tag, tag_photo
    from ppa.organization_views import save_organization_view
    from ppa.ui.organization_discovery_dialog import OrganizationDiscoveryDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    items = catalogue.grid_items(conn)
    pids = tuple(dict.fromkeys(i.photo_id for i in items[:2]))
    album = create_album(conn, library_id=lib_id, name="Holiday")
    tag = create_tag(conn, library_id=lib_id, name="Family")
    for pid in pids:
        add_photo_to_album(conn, album.id, pid); tag_photo(conn, tag.id, pid)
    saved = save_organization_view(conn, library_id=lib_id, name="Holiday family", album_ids=[album.id], tag_ids=[tag.id])
    dialog = OrganizationDiscoveryDialog(cfg.db_path, build_album_home(conn, library_id=lib_id),
                                         build_tag_home(conn, library_id=lib_id), None,
                                         cache_dir=tmp_path / "discover-thumbs")
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Organisational Discovery"
        assert dialog._album_list.count() == 1 and dialog._tag_list.count() == 1
        assert dialog._saved.count() == 2
        dialog._saved.setCurrentIndex(dialog._saved.findData(saved.id)); app.processEvents()
        assert dialog._album_list.item(0).isSelected() and dialog._tag_list.item(0).isSelected()
        dialog._album_list.item(0).setSelected(True); dialog._tag_list.item(0).setSelected(True)
        app.processEvents()
        assert dialog._browse.isEnabled()
        assert "Album: Holiday" in dialog._recipe.text() and "Tag: Family" in dialog._recipe.text()
    finally:
        dialog.close(); conn.close()


def test_phase9_organization_health_dialog_constructs(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.organization import create_album, create_tag
    from ppa.organization_health import build_organization_health
    from ppa.ui.organization_health_dialog import OrganizationHealthDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    create_album(conn, library_id=lib_id, name="Empty")
    create_tag(conn, library_id=lib_id, name="Unused")
    health = build_organization_health(conn, library_id=lib_id)
    dialog = OrganizationHealthDialog(conn, cfg.db_path, health, None,
                                      cache_dir=tmp_path / "health-thumbs")
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Organisation Health"
        assert health.unorganized_count > 0
        assert len(health.empty_album_ids) == 1
        assert len(health.unused_tag_ids) == 1
    finally:
        dialog.close(); conn.close()


def test_phase9_assisted_organization_dialog_constructs_with_review_candidates(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.organization import create_album, create_tag, bulk_add_photos_to_album, bulk_tag_photos
    from ppa.organization_suggestions import build_organization_suggestions
    from ppa.ui.organization_suggestions_dialog import OrganizationSuggestionsDialog

    cfg = _config_with_library(tmp_path)
    # The common smoke fixture starts with three photos; add two so the
    # conservative 5-photo suggestion threshold is genuinely exercised.
    libpath = tmp_path / "library"
    Image.new("RGB", (60, 40), "yellow").save(libpath / "IMG_0004.jpg")
    Image.new("RGB", (60, 40), "purple").save(libpath / "IMG_0005.jpg")
    conn = connect(cfg.db_path); scan_library(conn, libpath)
    lib_id = catalogue.list_libraries(conn)[0].id
    pids = tuple(sorted({i.photo_id for i in catalogue.grid_items(conn)}))
    album = create_album(conn, library_id=lib_id, name="Family Set")
    tag = create_tag(conn, library_id=lib_id, name="Family")
    bulk_add_photos_to_album(conn, album.id, pids)
    bulk_tag_photos(conn, tag.id, pids[:4])
    view = build_organization_suggestions(conn, library_id=lib_id)
    dialog = OrganizationSuggestionsDialog(conn, cfg.db_path, view, None,
                                           cache_dir=tmp_path / "suggest-thumbs")
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Assisted Organisation"
        assert dialog._table.rowCount() == 1
        assert dialog._review.isEnabled() and dialog._apply.isEnabled()
        assert "1 unique review candidate" in dialog._status.text()
    finally:
        dialog.close(); conn.close()


def test_phase9_suggestion_review_controls_construct(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.organization import create_album, create_tag, bulk_add_photos_to_album, bulk_tag_photos
    from ppa.organization_suggestions import build_organization_suggestions, dismiss_organization_suggestion
    from ppa.scanner import scan_library
    from ppa.ui.organization_suggestions_dialog import OrganizationSuggestionsDialog, SuggestionReviewsDialog

    cfg = _config_with_library(tmp_path)
    libpath = tmp_path / "library"
    Image.new("RGB", (60, 40), "orange").save(libpath / "IMG_0004.jpg")
    Image.new("RGB", (60, 40), "pink").save(libpath / "IMG_0005.jpg")
    conn = connect(cfg.db_path); scan_library(conn, libpath)
    lib_id = catalogue.list_libraries(conn)[0].id
    pids = tuple(sorted({i.photo_id for i in catalogue.grid_items(conn)}))
    album = create_album(conn, library_id=lib_id, name="Review Set")
    tag = create_tag(conn, library_id=lib_id, name="Family")
    bulk_add_photos_to_album(conn, album.id, pids); bulk_tag_photos(conn, tag.id, pids[:4])
    view = build_organization_suggestions(conn, library_id=lib_id)
    dialog = OrganizationSuggestionsDialog(conn, cfg.db_path, view, None,
                                           cache_dir=tmp_path / "review-thumbs")
    try:
        app.processEvents()
        assert dialog._dismiss.isEnabled()
        assert dialog._history.isEnabled()
        review = dismiss_organization_suggestion(conn, view.suggestions[0], note="Checked")
        history = SuggestionReviewsDialog(cfg.db_path, lib_id, (review,), dialog)
        try:
            app.processEvents()
            assert history.windowTitle() == "Reviewed Organisation Suggestions"
            assert history._table.rowCount() == 1
            assert history._restore.isEnabled()
        finally:
            history.close()
    finally:
        dialog.close(); conn.close()


def test_phase9_organization_activity_dialog_constructs_and_marks_safe_undo(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.organization import create_album, add_photo_to_album
    from ppa.organization_activity import build_organization_activity
    from ppa.ui.organization_activity_dialog import OrganizationActivityDialog

    cfg = _config_with_library(tmp_path)
    conn = connect(cfg.db_path)
    lib_id = catalogue.list_libraries(conn)[0].id
    pid = catalogue.grid_items(conn)[0].photo_id
    album = create_album(conn, library_id=lib_id, name="Activity Test")
    add_photo_to_album(conn, album.id, pid)
    view = build_organization_activity(conn, library_id=lib_id)
    dialog = OrganizationActivityDialog(cfg.db_path, view, None)
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Organisation Activity"
        assert dialog._table.rowCount() >= 2
        assert dialog._undo.isEnabled()
        assert "Added Photo" in dialog._table.item(0, 3).text()
    finally:
        dialog.close(); conn.close()


def test_phase10_duplicate_lineage_review_constructs_and_compares_exact_copies(app, tmp_path: Path) -> None:
    from ppa import catalogue
    from ppa.duplicate_lineage import add_lineage, build_duplicate_identity
    from ppa.ui.duplicate_lineage_dialog import DuplicateLineageDialog, SideBySidePreviewDialog

    cfg = _config_with_library(tmp_path)
    libpath = tmp_path / "library"
    # Make a byte-identical physical copy; scanner should attach it to the same
    # logical Photo rather than inventing a derivative relationship.
    (libpath / "IMG_0001_COPY.jpg").write_bytes((libpath / "IMG_0001.jpg").read_bytes())
    conn = connect(cfg.db_path)
    scan_library(conn, libpath)
    lib_id = catalogue.list_libraries(conn)[0].id
    view = build_duplicate_identity(conn, library_id=lib_id)
    assert len(view.sets) >= 1
    exact = view.sets[0]
    assert exact.copy_count >= 2

    # Add an explicit human lineage relation between two genuinely distinct Photos.
    photo_ids = sorted({item.photo_id for item in catalogue.grid_items(conn)})
    parent = exact.photo_id
    child = next(pid for pid in photo_ids if pid != parent)
    add_lineage(conn, parent_photo_id=parent, child_photo_id=child, relation_type="edited_variant")

    dialog = DuplicateLineageDialog(conn, lib_id, view, None)
    compare = None
    try:
        app.processEvents()
        assert dialog.windowTitle() == "Duplicates & Lineage"
        assert dialog.tabs.count() == 5
        assert dialog.exact_tree.topLevelItemCount() >= 1
        assert dialog.lineage_table.rowCount() == 1
        compare = SideBySidePreviewDialog(conn, exact.copies[0].file_id, exact.copies[1].file_id, dialog)
        compare.show(); app.processEvents()
        assert compare.windowTitle() == "Compare Exact Copies"
    finally:
        if compare is not None:
            compare.close()
        dialog.close(); conn.close()


def test_phase10_divergence_investigation_dialog_constructs_from_revision_evidence(app, tmp_path: Path) -> None:
    from ppa.db import connect
    from ppa.divergence_investigation import investigate_identity_divergence
    from ppa.ui.duplicate_lineage_dialog import DivergenceInvestigationDialog
    conn = connect(tmp_path / "divergence.sqlite")
    root = tmp_path / "lib"; root.mkdir()
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    conn.execute("INSERT INTO photos(id,created_at) VALUES ('p','x')")
    for fid,sha in [('f1','bbb'),('f2','aaa')]:
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256) VALUES (?,?,?,?,1,'2020','2021',?,?)",(fid,'p',str(root/(fid+'.jpg')),fid+'.jpg',lid,sha))
    conn.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at,superseded_at) VALUES ('r1','f1','aaa',1,'2020','2021')")
    conn.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES ('r2','f1','bbb',1,'2021')")
    conn.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES ('r3','f2','aaa',1,'2020')")
    conn.execute("UPDATE files SET current_revision_id='r2' WHERE id='f1'"); conn.execute("UPDATE files SET current_revision_id='r3' WHERE id='f2'"); conn.commit()
    view = investigate_identity_divergence(conn, library_id=lid, photo_id='p')
    dialog = DivergenceInvestigationDialog(view)
    try:
        dialog.show(); app.processEvents()
        assert dialog.windowTitle() == "Identity Divergence Investigation"
        assert dialog.evidence_tree.topLevelItemCount() == 2
        assert "MODIFIED IN PLACE" in dialog.evidence_tree.topLevelItem(0).text(3)
    finally:
        dialog.close(); conn.close()


def test_phase10_controlled_identity_split_ui_constructs_for_divergence(app, tmp_path: Path) -> None:
    from ppa.db import connect
    from ppa.duplicate_lineage import build_duplicate_identity
    from ppa.ui.duplicate_lineage_dialog import DuplicateLineageDialog

    conn = connect(tmp_path / "split-ui.sqlite")
    root = tmp_path / "lib-split"; root.mkdir()
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid = conn.execute("SELECT id FROM libraries").fetchone()[0]
    conn.execute("INSERT INTO photos(id,created_at) VALUES ('p','x')")
    from ppa.hashing import sha256_file
    for fid,color in [('fa','red'),('fb','blue')]:
        path=root/(fid+'.jpg'); Image.new('RGB',(16,12),color=color).save(path)
        sha=sha256_file(path); size=path.stat().st_size
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES (?,?,?,?,?,'x','x',?,?, 'present','ok')",(fid,'p',str(path),path.name,size,lid,sha))
        rid='r-'+fid
        conn.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES (?,?,?,?,'x')",(rid,fid,sha,size))
        conn.execute("UPDATE files SET current_revision_id=? WHERE id=?",(rid,fid))
    conn.commit()
    view=build_duplicate_identity(conn,library_id=lid)
    dialog=DuplicateLineageDialog(conn,lid,view,None)
    try:
        dialog.show(); app.processEvents()
        assert len(view.divergences)==1
        assert dialog.split_identity_btn.text()=="Split selected hash cohort…"
        assert dialog.divergence_tree.topLevelItemCount()==1
        assert dialog.divergence_tree.topLevelItem(0).childCount()==2
    finally:
        dialog.close(); conn.close()


def test_phase10_identity_resolution_review_ui_constructs_and_marks_recovery(app, tmp_path: Path) -> None:
    from ppa.db import connect
    from ppa.duplicate_lineage import build_duplicate_identity
    from ppa.identity_resolution import plan_identity_split, execute_identity_split, review_identity_resolution
    from ppa.ui.duplicate_lineage_dialog import DuplicateLineageDialog, IdentityResolutionReviewDialog
    conn=connect(tmp_path/'resolution-ui.sqlite')
    root=tmp_path/'lib-resolution'; root.mkdir()
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries").fetchone()[0]
    conn.execute("INSERT INTO photos(id,created_at) VALUES ('p','x')")
    from ppa.hashing import sha256_file
    for fid,color in [('fa','red'),('fb','blue')]:
        path=root/(fid+'.jpg'); Image.new('RGB',(16,12),color=color).save(path)
        sha=sha256_file(path); size=path.stat().st_size
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES (?,?,?,?,?,'x','x',?,?, 'present','ok')",(fid,'p',str(path),path.name,size,lid,sha))
        rid='r-'+fid
        conn.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES (?,?,?,?,'x')",(rid,fid,sha,size))
        conn.execute("UPDATE files SET current_revision_id=? WHERE id=?",(rid,fid))
    conn.commit()
    split=execute_identity_split(conn,plan_identity_split(conn,library_id=lid,source_photo_id='p',file_ids=('fa',)))
    view=build_duplicate_identity(conn,library_id=lid)
    dialog=DuplicateLineageDialog(conn,lid,view,None)
    review_dialog=IdentityResolutionReviewDialog(review_identity_resolution(conn,split.resolution_id),dialog)
    try:
        dialog.show(); review_dialog.show(); app.processEvents()
        assert dialog.tabs.count()==5
        assert dialog.resolution_table.rowCount()==1
        dialog.resolution_table.selectRow(0); app.processEvents()
        assert dialog.recombine_resolution_btn.isEnabled()
        assert review_dialog.windowTitle()=="Identity Resolution Review"
        assert review_dialog.topology_tree.topLevelItemCount()==3
    finally:
        review_dialog.close(); dialog.close(); conn.close()


def test_phase10_identity_health_tab_constructs_and_prioritises(app, tmp_path: Path) -> None:
    from ppa.db import connect
    from ppa.duplicate_lineage import build_duplicate_identity
    from ppa.identity_health import build_identity_health
    from ppa.ui.duplicate_lineage_dialog import DuplicateLineageDialog
    conn=connect(tmp_path/'health-ui.sqlite')
    root=tmp_path/'lib-health'; root.mkdir()
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries").fetchone()[0]
    for pid in ('p1','p2','p3'):
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",(pid,))
    for fid,pid,sha in [('a','p1','same'),('b','p2','same'),('c','p3','x'),('d','p3','y')]:
        path=root/(fid+'.jpg'); path.write_bytes(fid.encode())
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES (?,?,?,?,1,'x','x',?,?, 'present','ok')",(fid,pid,str(path),path.name,lid,sha))
    conn.commit()
    identity=build_duplicate_identity(conn,library_id=lid); health=build_identity_health(conn,library_id=lid)
    dialog=DuplicateLineageDialog(conn,lid,identity,None,identity_health=health)
    try:
        dialog.show(); app.processEvents()
        assert dialog.tabs.count()==5
        assert dialog.tabs.tabText(4).startswith('Identity Health')
        assert dialog.identity_health_table.rowCount()>=2
        assert dialog.identity_health_table.item(0,0).text()=='P0'
    finally:
        dialog.close(); conn.close()


def test_phase10_competing_identity_investigation_dialog_constructs(app, tmp_path: Path) -> None:
    from ppa.db import connect
    from ppa.competing_identity import investigate_competing_identity
    from ppa.duplicate_lineage import build_duplicate_identity
    from ppa.identity_health import build_identity_health
    from ppa.ui.duplicate_lineage_dialog import DuplicateLineageDialog, CompetingIdentityInvestigationDialog
    conn=connect(tmp_path/'competing-ui.sqlite')
    root=tmp_path/'lib-competing'; root.mkdir()
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries").fetchone()[0]
    for pid in ('p1','p2'): conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'2020')",(pid,))
    for fid,pid in [('a','p1'),('b','p2')]:
        path=root/(fid+'.jpg'); path.write_bytes(fid.encode())
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES (?,?,?,?,1,'2020','2021',?,'same','present','ok')",(fid,pid,str(path),path.name,lid))
        rid='r'+fid; conn.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES (?,?,'same',1,'2020')",(rid,fid)); conn.execute("UPDATE files SET current_revision_id=? WHERE id=?",(rid,fid))
    conn.commit()
    investigation=investigate_competing_identity(conn,library_id=lid,sha256='same')
    identity=build_duplicate_identity(conn,library_id=lid); health=build_identity_health(conn,library_id=lid)
    dialog=DuplicateLineageDialog(conn,lid,identity,None,identity_health=health)
    evidence=CompetingIdentityInvestigationDialog(investigation,dialog,conn=conn)
    try:
        dialog.show(); evidence.show(); app.processEvents()
        assert dialog.tabs.count()==5
        # This fixture creates coherent current FileRevisions for both Files, so
        # current-byte identity is verified. The first issue is therefore the
        # competing-identity P1, not the P0 unverified-current gate.
        assert dialog.identity_health_table.item(0,0).text()=='P1'
        dialog.identity_health_table.selectRow(0); app.processEvents()
        assert dialog.investigate_competing_btn.text()=='Investigate competing identity…'
        assert evidence.windowTitle()=='Competing Identity Investigation'
        assert evidence.evidence_tree.topLevelItemCount()==2
        assert evidence.merge_controls.isVisible()
        assert evidence.merge_btn.text()=='Merge competing identities…'
    finally:
        evidence.close(); dialog.close(); conn.close()


def test_phase11_workspace_navigation_replaces_flat_toolbar(app, tmp_path: Path) -> None:
    """Phase 11 keeps every command reachable without a 20+ action strip."""
    from ppa.ui.main_window import MainWindow

    config = _config_with_library(tmp_path)
    win = MainWindow(config)
    try:
        assert list(win._workspace_buttons) == [
            "Library", "Timeline", "Organisation", "Identity", "Diagnostics"
        ]

        expected = {
            "Library": {"Add Library…", "Libraries…", "Scan", "Verify", "Archive Health", "Extract Metadata"},
            "Timeline": {"Timeline", "Family History", "Date Review", "Unresolved Memories"},
            "Organisation": {
                "Albums & Tags…", "Albums", "Tags", "Discover",
                "Assisted Organisation", "Organisation Health",
                "Organisation Activity", "Export Organisation Report…",
            },
            "Identity": {"Duplicates & Lineage"},
            "Diagnostics": {
                "Pilot Audit", "Pilot Session…", "Activity Log…",
                "Activity Runs…", "Export Diagnostics…",
            },
        }
        for label, names in expected.items():
            actual = {a.text() for a in win._workspace_menus[label].actions() if not a.isSeparator()}
            assert actual == names

        # The toolbar itself contains only the global Refresh QAction; feature
        # actions live inside workspace menus instead of forming a giant strip.
        direct_action_text = {a.text() for a in win.findChild(QToolBar, "MainWorkspaceToolbar").actions()}
        assert "Refresh" in direct_action_text
        assert "Scan" not in direct_action_text
        assert "Albums" not in direct_action_text
        assert "Export Diagnostics…" not in direct_action_text
    finally:
        win._registry.shutdown()
        win.close()



def test_phase11_workspace_menu_entries_dispatch_canonical_actions(app, tmp_path: Path) -> None:
    """Workspace entries must actually dispatch, not merely render command labels."""
    from ppa.ui.main_window import MainWindow

    config = _config_with_library(tmp_path)
    win = MainWindow(config)
    try:
        cases = [
            ("Library", win._act_scan),
            ("Timeline", win._act_timeline),
            ("Organisation", win._act_albums),
            ("Identity", win._act_duplicates_lineage),
            ("Diagnostics", win._act_activity_log),
        ]
        for workspace, command in cases:
            proxy = next(
                action for action in win._workspace_menus[workspace].actions()
                if not action.isSeparator() and action.text() == command.text()
            )
            assert proxy is not command
            assert proxy.isEnabled() == command.isEnabled()

            # Isolate dispatch from the command's modal/worker production slot.
            # The proxy must call QAction.trigger(), which is proven by the
            # canonical action's triggered signal firing exactly once.
            command.triggered.disconnect()
            seen = []
            command.triggered.connect(lambda checked=False, bucket=seen: bucket.append(True))
            proxy.trigger()
            app.processEvents()
            assert seen == [True]

            # Menu-local enabled state follows the canonical command action.
            command.setEnabled(False)
            app.processEvents()
            assert not proxy.isEnabled()
            command.setEnabled(True)
            app.processEvents()
            assert proxy.isEnabled()
    finally:
        win._registry.shutdown()
        win.close()


def test_phase11_command_palette_filters_dispatches_and_respects_command_state(app, tmp_path: Path) -> None:
    """Phase 11.1 palette is a searchable proxy over canonical QActions."""
    from PySide6.QtGui import QKeySequence
    from ppa.ui.command_palette_dialog import CommandPaletteDialog
    from ppa.ui.main_window import MainWindow

    config = _config_with_library(tmp_path)
    win = MainWindow(config)
    try:
        commands = win._palette_commands()
        assert len(commands) == 24
        assert [c.workspace for c in commands[:5]] == ["Library"] * 5
        assert {c.label for c in commands} >= {
            "Scan", "Archive Health", "Timeline", "Albums & Tags…", "Organisation Health", "Duplicates & Lineage", "Activity Log…"
        }
        assert win._act_command_palette.shortcut() == QKeySequence("Ctrl+Shift+P")
        assert [sc.key() for sc in win._workspace_shortcuts] == [
            QKeySequence("Alt+1"), QKeySequence("Alt+2"), QKeySequence("Alt+3"),
            QKeySequence("Alt+4"), QKeySequence("Alt+5"),
        ]

        dialog = CommandPaletteDialog(commands, win)
        try:
            dialog.search.setText("organisation health")
            app.processEvents()
            # Phase 11.2 description search can legitimately surface additional
            # relevant commands (for example the organisation-health report).
            # Exact command-name matches must rank first rather than being the
            # only permitted result.
            assert dialog.list.count() >= 1
            assert dialog.current_command().action is win._act_org_health

            # Prove execution is through the canonical QAction, not a parallel handler.
            win._act_org_health.triggered.disconnect()
            seen = []
            win._act_org_health.triggered.connect(lambda checked=False: seen.append(True))
            dialog._run_current()
            app.processEvents()
            assert seen == [True]
        finally:
            dialog.close()

        disabled = CommandPaletteDialog(win._palette_commands(), win)
        try:
            win._act_scan.setEnabled(False)
            disabled.search.setText("scan")
            app.processEvents()
            assert disabled.list.count() == 1
            assert "unavailable" in disabled.list.item(0).text().lower()
            assert not disabled.run_button.isEnabled()
            win._act_scan.setEnabled(True)
            app.processEvents()
            assert "unavailable" not in disabled.list.item(0).text().lower()
            assert disabled.run_button.isEnabled()
        finally:
            disabled.close()
    finally:
        win._registry.shutdown()
        win.close()


def test_phase11_navigation_polish_descriptions_and_recent_palette_order(app, tmp_path: Path) -> None:
    """Phase 11.2 adds explanatory command metadata and session-local recents."""
    from PySide6.QtCore import Qt
    from ppa.ui.command_palette_dialog import CommandPaletteDialog
    from ppa.ui.main_window import MainWindow

    config = _config_with_library(tmp_path)
    win = MainWindow(config)
    try:
        commands = win._palette_commands()
        by_label = {command.label: command for command in commands}
        assert by_label["Verify"].description
        assert "integrity" in by_label["Verify"].description.casefold()
        assert by_label["Duplicates & Lineage"].description
        assert win._act_verify.toolTip() == by_label["Verify"].description
        assert win._workspace_menu_actions["Library"][3].toolTip() == by_label["Verify"].description

        # Descriptions are searchable, not merely decorative.
        searchable = CommandPaletteDialog(commands, win)
        try:
            searchable.search.setText("integrity hashes")
            app.processEvents()
            assert searchable.list.count() == 1
            assert searchable.current_command().label == "Verify"
        finally:
            searchable.close()

        # Recent ordering applies only to an empty query and remains bounded.
        recent = ["Organisation Health", "Timeline", "Scan"]
        dialog = CommandPaletteDialog(commands, win, recent_labels=recent)
        try:
            shown = [
                dialog._commands[dialog.list.item(i).data(Qt.ItemDataRole.UserRole)].label
                for i in range(3)
            ]
            assert shown == recent
            assert "recent" in dialog.list.item(0).text().casefold()
            dialog.search.setText("scan")
            app.processEvents()
            assert dialog.list.count() == 1
            assert "recent" not in dialog.list.item(0).text().casefold()
        finally:
            dialog.close()

        # Main-window recall is session-local, MRU, de-duplicated, and capped.
        for label in ["Scan", "Timeline", "Albums", "Tags", "Verify", "Scan"]:
            win._remember_palette_command(by_label[label])
        assert win._recent_palette_labels == ["Scan", "Verify", "Tags", "Albums", "Timeline"]
    finally:
        win._registry.shutdown()
        win.close()


def test_phase12_archive_health_navigation_and_dialog_are_read_only(app, tmp_path: Path) -> None:
    """Phase 12.2 exposes copy/storage/origin evidence without creating new authority."""
    from ppa.archive_health import build_archive_health
    from ppa.ui.archive_health_dialog import ArchiveHealthDialog
    from ppa.ui.main_window import MainWindow

    config = _config_with_library(tmp_path)
    conn = connect(config.db_path)
    lid = int(conn.execute("SELECT id FROM libraries ORDER BY id LIMIT 1").fetchone()["id"])
    health = build_archive_health(conn, library_id=lid)

    win = MainWindow(config)
    dialog = ArchiveHealthDialog(conn, config.db_path, health, win)
    try:
        assert "Archive Health" in {
            a.text() for a in win._workspace_menus["Library"].actions() if not a.isSeparator()
        }
        command = next(c for c in win._palette_commands() if c.label == "Archive Health")
        assert "copy coverage" in command.description.casefold()
        assert dialog.windowTitle() == "Backup & Archive Health"
        assert health.single_present_count == 3
    finally:
        dialog.close(); win._registry.shutdown(); win.close(); conn.close()


def test_phase13_recovery_planning_dialog_smoke(app) -> None:
    from types import SimpleNamespace
    from ppa.ui.recovery_planning_dialog import RecoveryPlanningDialog

    candidate = SimpleNamespace(
        qualified=True,
        library_id=1,
        path="/archive/donor.jpg",
        topology_class="distinct_filesystem_objects_same_device_id",
        rejection_reasons=(),
        physical_sha256="abc",
        expected_sha256="abc",
    )
    view = SimpleNamespace(
        file_id="target-file",
        path="/archive/target.jpg",
        expected_sha256="abc",
        target_state="still_mismatched",
        recovery_intent_resolution_id="resolution-1",
        candidates=(candidate,),
        notes=(),
    )
    plan = SimpleNamespace(
        donor_file_id="donor-file",
        donor_library_id=1,
        donor_path="/archive/donor.jpg",
        topology_class="distinct_filesystem_objects_same_device_id",
        evidence_fingerprint="fingerprint",
        proposed_action=("preserve suspect bytes", "restore expected bytes"),
    )
    dlg = RecoveryPlanningDialog(view, plan)
    captured = []
    dlg.proposal_requested.connect(lambda p, note: captured.append((p, note)))
    dlg.proposal_requested.emit(plan, "reviewed")
    app.processEvents()
    assert captured == [(plan, "reviewed")]
    assert "Dry Run" in dlg.windowTitle()
    dlg.close()


def test_phase13_plan_recovery_button_emits_file_id(app) -> None:
    from types import SimpleNamespace
    from PySide6.QtWidgets import QPushButton
    from ppa.ui.mismatch_investigation_dialog import MismatchInvestigationDialog

    inv = SimpleNamespace(
        file_id="target-file",
        verify_observed_sha256="badsha",
        verify_observed_at="2026-08-28T00:00:00Z",
        latest_resolution_action="retain_expected_recovery_needed",
        latest_resolution_at="2026-08-28T00:01:00Z",
        latest_resolution_note=None,
        notes=(),
        current_state="still_mismatched",
        expected_reference_status="unavailable",
        expected_reference_attested=False,
        expected_reference_path=None,
        expected_sha256="expectedsha",
        current_preview_path=None,
        current_observed_sha256="badsha",
    )
    dlg = MismatchInvestigationDialog(inv)
    captured = []
    dlg.recovery_planning_requested.connect(captured.append)
    button = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Plan recovery…")
    button.click(); app.processEvents()
    assert captured == ["target-file"]
    dlg.close()


def test_phase14_recovery_preservation_worker_smoke(app, tmp_path) -> None:
    from ppa.ui.workers import RecoveryPreservationWorker

    worker = RecoveryPreservationWorker(tmp_path / "catalogue.sqlite3", "proposal-1", "reviewed")
    assert worker._proposal_id == "proposal-1"
    assert worker._note == "reviewed"
    worker.deleteLater()
    app.processEvents()
