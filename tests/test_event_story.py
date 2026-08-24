from __future__ import annotations

from pathlib import Path

import pytest

from ppa.db import connect
from ppa.event_story import EVENT_STORY_SCHEMA, build_event_story
from ppa.events import add_event_member, create_event_from_cluster, remove_event_member, update_event_context
from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
from ppa.timeline_clusters import DayCount, TimelineCluster


def _library(conn, root: Path, ids=("a", "b", "c", "d")):
    root.mkdir()
    conn.execute("INSERT INTO libraries (root_display_path, root_canonical_path) VALUES (?, ?)", (str(root), str(root).casefold()))
    lid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    for fid in ids:
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?, 'x')", (f"p-{fid}",))
        conn.execute(
            "INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id) "
            "VALUES (?,?,?,?,1,'x','x',?)",
            (fid, f"p-{fid}", str(root / f"{fid}.jpg"), f"{fid}.jpg", lid),
        )
    conn.commit()
    return lid


def _cluster():
    return TimelineCluster(
        "cluster-story", "day_burst", "25 Dec 2004 · 3 photos", "2004-12-25", "2004-12-25", 3,
        ("a", "b", "c"), (), (DayCount("2004-12-25", 3),), "test",
    )


def _view(lid, items):
    lanes = {}
    for lane in ("placed", "range", "tentative", "unplaced"):
        ids = tuple(i.file_id for i in items if i.lane == lane)
        lanes[lane] = TimelineBucket(lane, len(ids), ids)
    scope = type("Scope", (), {"library_id": lid})()
    return TimelineView("ppa-timeline/1", "x", True, scope, tuple(items), lanes, ())


def _item(fid, lane, start=None, end=None, source="reconciled"):
    return TimelineItem(
        fid, f"{fid}.jpg", lane, source, start, end,
        "PROBABLY_VALID" if lane == "placed" else "QUESTIONABLE",
        None, None, None, False, False, f"reason-{fid}",
    )


def test_story_is_read_only_and_orders_by_current_chronology(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid = _library(conn, tmp_path / "lib")
    event = create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Christmas 2004")
    add_event_member(conn, event.id, "d")
    update_event_context(conn, event.id, story_text="Definitely Christmas Day 2004")
    view = _view(lid, (
        _item("a", "placed", "2004-12-25"),
        _item("b", "range", "2004-12-24", "2004-12-27", "confirmed_reconstruction"),
        _item("c", "tentative", "2004-12-26", source="proposed_reconstruction"),
        _item("d", "unplaced"),
    ))
    before = conn.total_changes
    story = build_event_story(conn, view, event.id)
    assert conn.total_changes == before
    assert story.schema == EVENT_STORY_SCHEMA and story.read_only is True
    assert [p.file_id for p in story.photos] == ["b", "a", "c", "d"]
    assert story.lane_counts == {"placed": 1, "range": 1, "tentative": 1, "unplaced": 1}
    assert story.photos[-1].member_role == "human_added"


def test_story_narrative_never_changes_member_lane_or_date(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid = _library(conn, tmp_path / "lib")
    event = create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Christmas")
    update_event_context(conn, event.id, story_text="Absolutely taken on 25 December 1999")
    view = _view(lid, (
        _item("a", "unplaced"), _item("b", "placed", "2004-12-25"), _item("c", "placed", "2004-12-25"),
    ))
    story = build_event_story(conn, view, event.id)
    a = next(p for p in story.photos if p.file_id == "a")
    assert a.lane == "unplaced" and a.start_date is None


def test_story_uses_current_explicit_membership_not_original_cluster(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid = _library(conn, tmp_path / "lib")
    event = create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Christmas")
    remove_event_member(conn, event.id, "b")
    add_event_member(conn, event.id, "d")
    view = _view(lid, tuple(_item(fid, "placed", "2004-12-25") for fid in ("a", "b", "c", "d")))
    story = build_event_story(conn, view, event.id)
    assert [p.file_id for p in story.photos] == ["a", "c", "d"]


def test_story_scope_does_not_fabricate_missing_members(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid = _library(conn, tmp_path / "lib")
    event = create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Christmas")
    # Narrow Timeline scope only contains A; B/C remain durable Event members
    # but do not get invented chronology rows in this Story projection.
    story = build_event_story(conn, _view(lid, (_item("a", "placed", "2004-12-25"),)), event.id)
    assert [p.file_id for p in story.photos] == ["a"]
    assert story.event.file_ids == ("a", "b", "c")


def test_story_rejects_event_from_different_library(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid1 = _library(conn, tmp_path / "lib1", ("a", "b", "c"))
    event = create_event_from_cluster(conn, library_id=lid1, cluster=_cluster(), name="Christmas")
    lid2 = _library(conn, tmp_path / "lib2", ("x",))
    with pytest.raises(ValueError, match="Timeline library"):
        build_event_story(conn, _view(lid2, (_item("x", "placed", "2020-01-01"),)), event.id)


def test_story_json_is_deterministic(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid = _library(conn, tmp_path / "lib")
    event = create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Christmas")
    view = _view(lid, tuple(_item(fid, "placed", "2004-12-25") for fid in ("a", "b", "c")))
    a = build_event_story(conn, view, event.id).to_json(pretty=False)
    b = build_event_story(conn, view, event.id).to_json(pretty=False)
    assert a == b


def test_story_honours_human_presentation_order_without_changing_dates(tmp_path):
    from ppa.events import set_event_presentation_order
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib')
    event=create_event_from_cluster(conn,library_id=lid,cluster=_cluster(),name='Christmas')
    view=_view(lid,(_item('a','placed','2004-12-24'),_item('b','placed','2004-12-25'),_item('c','placed','2004-12-26')))
    set_event_presentation_order(conn,event.id,('c','a','b'))
    story=build_event_story(conn,view,event.id)
    assert [p.file_id for p in story.photos] == ['c','a','b']
    assert [p.start_date for p in story.photos] == ['2004-12-26','2004-12-24','2004-12-25']
