from __future__ import annotations

from pathlib import Path

import pytest

from ppa.db import connect
from ppa.event_navigation import EVENT_BROWSE_SCHEMA, build_event_browse_index
from ppa.events import create_event_from_cluster
from ppa.timeline_clusters import DayCount, TimelineCluster


def _library(conn, root: Path, ids=("a", "b", "c", "d", "e", "f")):
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


def _cluster(key, day, ids):
    return TimelineCluster(key, "day_burst", f"{day} · {len(ids)} photos", day, day, len(ids),
                           tuple(ids), (), (DayCount(day, len(ids)),), "test")


def test_event_browse_is_read_only_chronological_and_linked(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid = _library(conn, tmp_path / "lib")
    later = create_event_from_cluster(conn, library_id=lid, cluster=_cluster("later", "2005-02-01", ("d","e","f")), name="Later")
    earlier = create_event_from_cluster(conn, library_id=lid, cluster=_cluster("earlier", "2004-12-25", ("a","b","c")), name="Earlier")
    before = conn.total_changes
    index = build_event_browse_index(conn, library_id=lid)
    assert conn.total_changes == before
    assert index.schema == EVENT_BROWSE_SCHEMA and index.read_only is True
    assert [c.event_id for c in index.cards] == [earlier.id, later.id]
    assert index.cards[0].previous_event_id is None and index.cards[0].next_event_id == later.id
    assert index.cards[1].previous_event_id == earlier.id and index.cards[1].next_event_id is None


def test_event_browse_groups_by_start_year_and_is_deterministic(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid = _library(conn, tmp_path / "lib")
    create_event_from_cluster(conn, library_id=lid, cluster=_cluster("a", "2004-01-01", ("a","b","c")), name="A")
    create_event_from_cluster(conn, library_id=lid, cluster=_cluster("b", "2004-12-25", ("d","e","f")), name="B")
    a = build_event_browse_index(conn, library_id=lid)
    b = build_event_browse_index(conn, library_id=lid)
    assert a.to_json(pretty=False) == b.to_json(pretty=False)
    assert [(g.year, g.count) for g in a.years] == [(2004, 2)]


def test_same_day_events_have_stable_name_then_id_order(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    lid = _library(conn, tmp_path / "lib")
    z = create_event_from_cluster(conn, library_id=lid, cluster=_cluster("z", "2004-12-25", ("a","b","c")), name="Zulu")
    a = create_event_from_cluster(conn, library_id=lid, cluster=_cluster("a", "2004-12-25", ("d","e","f")), name="Alpha")
    index = build_event_browse_index(conn, library_id=lid)
    assert [c.event_id for c in index.cards] == [a.id, z.id]


def test_unknown_library_and_unknown_event_fail_closed(tmp_path):
    conn = connect(tmp_path / "ppa.sqlite")
    with pytest.raises(ValueError, match="unknown library"):
        build_event_browse_index(conn, library_id=999)
    lid = _library(conn, tmp_path / "lib")
    index = build_event_browse_index(conn, library_id=lid)
    with pytest.raises(ValueError, match="not present"):
        index.card("missing")
