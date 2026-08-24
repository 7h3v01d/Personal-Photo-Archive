from __future__ import annotations

from pathlib import Path
import pytest

from ppa.db import connect
from ppa.event_home import build_event_home
from ppa.event_search import EVENT_SEARCH_SCHEMA, build_event_search_index, search_event_index
from ppa.events import create_event_from_cluster, update_event_context, update_event_note
from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
from ppa.timeline_clusters import DayCount, TimelineCluster


def _library(conn, root: Path, ids=("a","b","c","d","e","f","g","h","i")):
    root.mkdir(); conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root), str(root).casefold()))
    lid=conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    for fid in ids:
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?, 'x')", (f"p-{fid}",))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id) VALUES (?,?,?,?,1,'x','x',?)", (fid,f"p-{fid}",str(root/f"{fid}.jpg"),f"{fid}.jpg",lid))
    conn.commit(); return lid


def _cluster(key, day, ids):
    return TimelineCluster(key,"day_burst",f"{day} · {len(ids)} photos",day,day,len(ids),tuple(ids),(),(DayCount(day,len(ids)),),"test")


def _item(fid, day):
    return TimelineItem(fid,f"{fid}.jpg","placed","reconciled",day,None,"PROBABLY_VALID",None,None,None,False,False,"test")


def _view(lid, items):
    lanes={lane:TimelineBucket(lane,len(tuple(i for i in items if i.lane==lane)),tuple(i.file_id for i in items if i.lane==lane)) for lane in ("placed","range","tentative","unplaced")}
    scope=type("Scope",(),{"library_id":lid})(); return TimelineView("ppa-timeline/1","x",True,scope,tuple(items),lanes,())


def _fixture(tmp_path):
    conn=connect(tmp_path/"ppa.sqlite"); lid=_library(conn,tmp_path/"lib")
    christmas=create_event_from_cluster(conn,library_id=lid,cluster=_cluster("x","2004-12-25",("a","b","c")),name="Christmas Lunch")
    trip=create_event_from_cluster(conn,library_id=lid,cluster=_cluster("y","2005-02-01",("d","e","f")),name="Sydney Trip")
    birthday=create_event_from_cluster(conn,library_id=lid,cluster=_cluster("z","2005-07-10",("g","h","i")),name="Birthday")
    update_event_context(conn,christmas.id,occasion_text="Christmas Day",place_text="Mum and Dad's house",people_text="Mum Dad Leon",description="Family presents")
    update_event_context(conn,trip.id,place_text="Sydney Harbour",people_text="Leon Maddie",story_text="We caught the ferry and walked around Circular Quay")
    update_event_context(conn,birthday.id,occasion_text="Birthday party",story_text="Chocolate cake and balloons")
    update_event_note(conn,birthday.id,"School friends came over")
    view=_view(lid,(_item("a","2004-12-25"),_item("b","2004-12-25"),_item("c","2004-12-25"),_item("d","2005-02-01"),_item("e","2005-02-01"),_item("f","2005-02-01"),_item("g","2005-07-10"),_item("h","2005-07-10"),_item("i","2005-07-10")))
    home=build_event_home(conn,view)
    return conn,lid,home,christmas,trip,birthday


def test_search_index_is_read_only_and_deterministic(tmp_path):
    conn,lid,home,*_=_fixture(tmp_path); before=conn.total_changes
    a=build_event_search_index(conn,home); b=build_event_search_index(conn,home)
    assert conn.total_changes==before and a.schema==EVENT_SEARCH_SCHEMA and a.read_only
    assert a.to_json(pretty=False)==b.to_json(pretty=False) and a.library_id==lid


def test_search_matches_all_human_context_fields_with_and_semantics(tmp_path):
    conn,_lid,home,christmas,trip,birthday=_fixture(tmp_path); idx=build_event_search_index(conn,home)
    assert [h.event_id for h in search_event_index(idx,text="mum presents").hits]==[christmas.id]
    assert [h.event_id for h in search_event_index(idx,text="circular quay").hits]==[trip.id]
    hit=search_event_index(idx,text="school friends").hits[0]
    assert hit.event_id==birthday.id and "note" in hit.matched_fields


def test_name_matches_rank_ahead_of_story_matches(tmp_path):
    conn,_lid,home,_christmas,trip,birthday=_fixture(tmp_path); idx=build_event_search_index(conn,home)
    update_event_context(conn,birthday.id,story_text="Our Sydney themed birthday")
    idx=build_event_search_index(conn,home)
    hits=search_event_index(idx,text="sydney").hits
    assert hits[0].event_id==trip.id and hits[0].score>hits[1].score and hits[1].event_id==birthday.id


def test_empty_search_preserves_family_history_order_and_year_filter(tmp_path):
    conn,_lid,home,christmas,trip,birthday=_fixture(tmp_path); idx=build_event_search_index(conn,home)
    assert [h.event_id for h in search_event_index(idx).hits]==[christmas.id,trip.id,birthday.id]
    assert [h.event_id for h in search_event_index(idx,year=2005).hits]==[trip.id,birthday.id]


def test_date_filters_use_event_span_overlap(tmp_path):
    conn,_lid,home,christmas,trip,birthday=_fixture(tmp_path); idx=build_event_search_index(conn,home)
    assert [h.event_id for h in search_event_index(idx,start_date="2005-01-01",end_date="2005-03-01").hits]==[trip.id]
    assert search_event_index(idx,end_date="2004-12-31").hits[0].event_id==christmas.id
    assert birthday.id not in {h.event_id for h in search_event_index(idx,end_date="2005-03-01").hits}


def test_invalid_search_filters_fail_closed(tmp_path):
    conn,_lid,home,*_=_fixture(tmp_path); idx=build_event_search_index(conn,home)
    with pytest.raises(ValueError,match="invalid start date"):
        search_event_index(idx,start_date="not-a-date")
    with pytest.raises(ValueError,match="must not precede"):
        search_event_index(idx,start_date="2005-02-01",end_date="2004-01-01")
    with pytest.raises(ValueError,match="year"):
        search_event_index(idx,year=0)
