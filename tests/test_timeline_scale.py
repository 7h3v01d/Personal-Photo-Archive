from ppa.pilot import PilotScope
from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
from ppa.timeline_scale import (
    density_buckets, filter_for_bucket, page_for_fraction, page_items,
)


def _item(fid, lane, start=None, end=None):
    source = "reconciled" if lane == "placed" else "proposed_reconstruction" if lane == "tentative" else "none"
    return TimelineItem(fid, fid + ".jpg", lane, source, start, end,
                        "PROBABLY_VALID", None, None, None, False, False, "x")


def _view():
    items = (
        _item("a", "placed", "1999-12-31"),
        _item("b", "placed", "2000-01-01"),
        _item("c", "range", "2004-12-24", "2004-12-27"),
        _item("d", "tentative", "2005-01-02"),
        _item("e", "placed", "2011-06-03"),
        _item("u", "unplaced"),
    )
    lanes = {
        "placed": TimelineBucket("placed", 3, ("a", "b", "e")),
        "range": TimelineBucket("range", 1, ("c",)),
        "tentative": TimelineBucket("tentative", 1, ("d",)),
        "unplaced": TimelineBucket("unplaced", 1, ("u",)),
    }
    return TimelineView("ppa-timeline/1", "fixed", True,
                        PilotScope(1, "/lib", None, None), items, lanes, ())


def test_density_buckets_are_deterministic_at_decade_year_month_scales():
    view = _view()
    assert [(b.key, b.count) for b in density_buckets(view, scale="decade")] == [
        ("1990s", 1), ("2000s", 3), ("2010s", 1)
    ]
    assert [(b.key, b.count) for b in density_buckets(view, scale="year")] == [
        ("1999", 1), ("2000", 1), ("2004", 1), ("2005", 1), ("2011", 1)
    ]
    assert [(b.key, b.count) for b in density_buckets(view, scale="month", lane="placed")] == [
        ("1999-12", 1), ("2000-01", 1), ("2011-06", 1)
    ]


def test_range_is_indexed_by_start_bucket_but_precision_is_preserved():
    view = _view()
    items = filter_for_bucket(view, bucket_key="2004-12")
    assert len(items) == 1
    assert items[0].lane == "range"
    assert items[0].start_date == "2004-12-24"
    assert items[0].end_date == "2004-12-27"


def test_decade_filter_and_lane_filter_do_not_leak_unplaced():
    view = _view()
    assert [i.file_id for i in filter_for_bucket(view, bucket_key="2000s")] == ["b", "c", "d"]
    assert [i.file_id for i in filter_for_bucket(view, bucket_key="2000s", lane="tentative")] == ["d"]


def test_paging_is_bounded_and_preserves_order():
    vals = tuple(range(251))
    first = page_items(vals, page=0, page_size=120)
    last = page_items(vals, page=2, page_size=120)
    assert first.items == tuple(range(120))
    assert first.total_pages == 3 and first.has_next and not first.has_previous
    assert last.items == tuple(range(240, 251))
    assert last.start_index == 240 and last.end_index == 251
    assert last.has_previous and not last.has_next


def test_scrubber_maps_stably_to_page_boundaries():
    assert page_for_fraction(1000, 0.0, page_size=100) == 0
    assert page_for_fraction(1000, 0.5, page_size=100) in {4, 5}
    assert page_for_fraction(1000, 1.0, page_size=100) == 9


def test_invalid_scale_bucket_page_and_fraction_fail_closed():
    import pytest
    view = _view()
    with pytest.raises(ValueError):
        density_buckets(view, scale="week")
    with pytest.raises(ValueError):
        filter_for_bucket(view, bucket_key="banana")
    with pytest.raises(ValueError):
        page_items((1, 2), page=2, page_size=1)
    with pytest.raises(ValueError):
        page_for_fraction(10, 1.1)
