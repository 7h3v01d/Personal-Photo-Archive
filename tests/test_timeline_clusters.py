from ppa.pilot import PilotScope
from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
from ppa.timeline_clusters import build_clusters, items_for_cluster


def _item(fid, lane, start=None, end=None):
    src = "reconciled" if lane == "placed" else "proposed_reconstruction" if lane == "tentative" else "confirmed_reconstruction" if lane == "range" else "none"
    return TimelineItem(fid, fid + ".jpg", lane, src, start, end,
                        "PROBABLY_VALID", None, None, None, False, False, "x")


def _view(items):
    lanes = {}
    for lane in ("placed", "range", "tentative", "unplaced"):
        ids = tuple(i.file_id for i in items if i.lane == lane)
        lanes[lane] = TimelineBucket(lane, len(ids), ids)
    return TimelineView("ppa-timeline/1", "fixed", True,
                        PilotScope(1, "/lib", None, None), tuple(items), lanes, ())


def test_same_day_burst_requires_authoritative_point_placements():
    items = [_item(f"p{i}", "placed", "2004-12-25") for i in range(4)]
    items += [_item(f"t{i}", "tentative", "2004-12-25") for i in range(8)]
    result = build_clusters(_view(items), generated_at="fixed")
    assert len(result.clusters) == 1
    c = result.clusters[0]
    assert c.kind == "day_burst" and c.authoritative_count == 4
    assert set(c.seed_file_ids) == {f"p{i}" for i in range(4)}
    assert set(c.context_file_ids) == {f"t{i}" for i in range(8)}


def test_tentative_and_ranges_cannot_seed_a_cluster():
    items = [_item(f"t{i}", "tentative", "2004-12-25") for i in range(10)]
    items += [_item("r", "range", "2004-12-24", "2004-12-27")]
    assert build_clusters(_view(items), generated_at="fixed").clusters == ()


def test_dense_adjacent_days_create_one_multi_day_cluster_and_suppress_day_duplicates():
    items = []
    for day in ("2004-12-24", "2004-12-25", "2004-12-26"):
        items += [_item(f"{day}-{i}", "placed", day) for i in range(4)]
    result = build_clusters(_view(items), generated_at="fixed")
    assert len(result.clusters) == 1
    c = result.clusters[0]
    assert c.kind == "dense_multi_day_run"
    assert c.start_date == "2004-12-24" and c.end_date == "2004-12-26"
    assert c.authoritative_count == 12
    assert [x.count for x in c.day_counts] == [4, 4, 4]


def test_sparse_adjacent_days_do_not_become_multi_day_event_like_group():
    items = [_item("a", "placed", "2004-12-24"), _item("b", "placed", "2004-12-25")]
    assert build_clusters(_view(items), generated_at="fixed").clusters == ()


def test_long_dense_streak_is_not_promoted_to_one_giant_event():
    items = []
    for d in range(1, 10):
        day = f"2004-01-{d:02d}"
        items += [_item(f"{d}-{i}", "placed", day) for i in range(4)]
    result = build_clusters(_view(items), generated_at="fixed")
    # Deliberately falls back to daily bursts rather than inventing a 9-day event.
    assert len(result.clusters) == 9
    assert all(c.kind == "day_burst" for c in result.clusters)


def test_range_overlapping_cluster_is_context_only_and_precision_survives():
    items = [_item(f"p{i}", "placed", "2004-12-25") for i in range(4)]
    items.append(_item("r", "range", "2004-12-24", "2004-12-27"))
    c = build_clusters(_view(items), generated_at="fixed").clusters[0]
    assert c.context_file_ids == ("r",)
    r = next(i for i in items if i.file_id == "r")
    assert r.start_date == "2004-12-24" and r.end_date == "2004-12-27"


def test_cluster_key_is_stable_under_input_ordering():
    items = [_item(f"p{i}", "placed", "2004-12-25") for i in range(4)]
    a = build_clusters(_view(items), generated_at="A").clusters[0]
    b = build_clusters(_view(list(reversed(items))), generated_at="B").clusters[0]
    assert a.key == b.key
    assert a.seed_file_ids == b.seed_file_ids


def test_cluster_filter_preserves_original_lanes_and_rejects_unknown_lane():
    import pytest
    items = [_item(f"p{i}", "placed", "2004-12-25") for i in range(4)]
    items += [_item("t", "tentative", "2004-12-25"), _item("r", "range", "2004-12-24", "2004-12-27")]
    view = _view(items)
    c = build_clusters(view, generated_at="fixed").clusters[0]
    assert [i.file_id for i in items_for_cluster(view, c, lane="placed")] == ["p0", "p1", "p2", "p3"]
    assert [i.file_id for i in items_for_cluster(view, c, lane="tentative")] == ["t"]
    assert [i.file_id for i in items_for_cluster(view, c, lane="range")] == ["r"]
    with pytest.raises(ValueError):
        items_for_cluster(view, c, lane="banana")
