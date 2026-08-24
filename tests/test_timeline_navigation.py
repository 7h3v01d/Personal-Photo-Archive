from dataclasses import replace

from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
from ppa.pilot import PilotScope
from ppa.timeline_navigation import build_navigation, filter_items


def _item(fid, lane, start=None, end=None):
    return TimelineItem(fid, fid + ".jpg", lane, "reconciled" if lane == "placed" else "none",
                        start, end, "PROBABLY_VALID", None, None, None, False, False, "x")


def _view():
    items = (
        _item("a", "placed", "2004-12-25"),
        _item("b", "range", "2004-12-24", "2004-12-27"),
        _item("c", "tentative", "2005-01-02"),
        _item("d", "unplaced"),
    )
    lanes = {
        "placed": TimelineBucket("placed", 1, ("a",)),
        "range": TimelineBucket("range", 1, ("b",)),
        "tentative": TimelineBucket("tentative", 1, ("c",)),
        "unplaced": TimelineBucket("unplaced", 1, ("d",)),
    }
    return TimelineView("ppa-timeline/1", "fixed", True,
                        PilotScope(1, "/lib", None, None), items, lanes, ())


def test_navigation_is_deterministic_and_keeps_ranges_as_one_photo():
    nav = build_navigation(_view())
    assert [(x.key, x.count) for x in nav] == [
        ("all", 3), ("2004", 2), ("2004-12", 2), ("2005", 1), ("2005-01", 1), ("unplaced", 1)
    ]


def test_filter_by_year_month_lane_and_unplaced():
    view = _view()
    assert [i.file_id for i in filter_items(view, nav_key="2004")] == ["a", "b"]
    assert [i.file_id for i in filter_items(view, nav_key="2004-12", lane="range")] == ["b"]
    assert [i.file_id for i in filter_items(view, nav_key="unplaced")] == ["d"]


def test_unknown_navigation_or_lane_fails_closed():
    import pytest
    view = _view()
    with pytest.raises(ValueError):
        filter_items(view, nav_key="banana")
    with pytest.raises(ValueError):
        filter_items(view, lane="banana")
