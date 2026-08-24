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
