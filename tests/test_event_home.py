from __future__ import annotations
from pathlib import Path
import pytest
from ppa.db import connect
from ppa.event_home import EVENT_HOME_SCHEMA, build_event_home
from ppa.events import add_event_member, create_event_from_cluster, update_event_context
from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
from ppa.timeline_clusters import DayCount, TimelineCluster


def _library(conn, root: Path, ids=("a","b","c","d","e","f","g")):
    root.mkdir(); conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root), str(root).casefold()))
    lid=conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    for fid in ids:
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?, 'x')", (f"p-{fid}",))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id) VALUES (?,?,?,?,1,'x','x',?)", (fid,f"p-{fid}",str(root/f"{fid}.jpg"),f"{fid}.jpg",lid))
    conn.commit(); return lid


def _cluster(key, day, ids):
    return TimelineCluster(key,"day_burst",f"{day} · {len(ids)} photos",day,day,len(ids),tuple(ids),(),(DayCount(day,len(ids)),),"test")


def _item(fid,lane="placed",start="2004-12-25"):
    return TimelineItem(fid,f"{fid}.jpg",lane,"reconciled",start if lane!="unplaced" else None,None,"PROBABLY_VALID" if lane=="placed" else "QUESTIONABLE",None,None,None,False,False,"test")


def _view(lid, items):
    lanes={}
    for lane in ("placed","range","tentative","unplaced"):
        ids=tuple(i.file_id for i in items if i.lane==lane); lanes[lane]=TimelineBucket(lane,len(ids),ids)
    scope=type("Scope",(),{"library_id":lid})(); return TimelineView("ppa-timeline/1","x",True,scope,tuple(items),lanes,())


def test_home_is_read_only_year_grouped_and_story_aware(tmp_path):
    conn=connect(tmp_path/"ppa.sqlite"); lid=_library(conn,tmp_path/"lib")
    e1=create_event_from_cluster(conn,library_id=lid,cluster=_cluster("x","2004-12-25",("a","b","c")),name="Christmas")
    e2=create_event_from_cluster(conn,library_id=lid,cluster=_cluster("y","2005-02-01",("d","e","f")),name="Trip")
    update_event_context(conn,e1.id,description="Family lunch and presents")
    view=_view(lid,tuple(_item(x) for x in "abcdef"))
    before=conn.total_changes; home=build_event_home(conn,view)
    assert conn.total_changes==before and home.schema==EVENT_HOME_SCHEMA and home.read_only
    assert [g.year for g in home.years]==[2004,2005]
    assert home.card(e1.id).snippet=="Family lunch and presents"
    assert home.card(e2.id).member_count==3


def test_cover_is_stable_seed_identity_not_chronology(tmp_path):
    conn=connect(tmp_path/"ppa.sqlite"); lid=_library(conn,tmp_path/"lib")
    event=create_event_from_cluster(conn,library_id=lid,cluster=_cluster("x","2004-12-25",("c","a","b")),name="Christmas")
    add_event_member(conn,event.id,"d")
    v1=_view(lid,(_item("c",start="2004-12-24"),_item("a",start="2004-12-26"),_item("b",start="2004-12-25"),_item("d",start="2004-12-23")))
    v2=_view(lid,(_item("c",start="2005-01-05"),_item("a",start="2004-12-20"),_item("b",start="2004-12-30"),_item("d",start="2004-12-19")))
    assert build_event_home(conn,v1).card(event.id).cover_file_id=="a"
    assert build_event_home(conn,v2).card(event.id).cover_file_id=="a"


def test_human_added_member_never_displaces_seed_cover(tmp_path):
    conn=connect(tmp_path/"ppa.sqlite"); lid=_library(conn,tmp_path/"lib")
    event=create_event_from_cluster(conn,library_id=lid,cluster=_cluster("x","2004-12-25",("b","c","d")),name="Christmas")
    add_event_member(conn,event.id,"a")
    home=build_event_home(conn,_view(lid,tuple(_item(x) for x in "abcd")))
    card=home.card(event.id)
    assert card.cover_file_id=="b" and card.cover_rule=="stable_authoritative_seed_file_id"


def test_home_preserves_lane_counts_and_does_not_promote_story(tmp_path):
    conn=connect(tmp_path/"ppa.sqlite"); lid=_library(conn,tmp_path/"lib")
    event=create_event_from_cluster(conn,library_id=lid,cluster=_cluster("x","2004-12-25",("a","b","c")),name="Christmas")
    update_event_context(conn,event.id,story_text="Definitely all taken on Christmas Day")
    view=_view(lid,(_item("a","unplaced"),_item("b"),_item("c","tentative","2004-12-25")))
    card=build_event_home(conn,view).card(event.id)
    assert card.lane_counts=={"placed":1,"range":0,"tentative":1,"unplaced":1}


def test_home_rejects_timeline_from_unknown_library(tmp_path):
    conn=connect(tmp_path/"ppa.sqlite")
    with pytest.raises(ValueError, match="unknown library"):
        build_event_home(conn,_view(999,()))


def test_home_json_is_deterministic(tmp_path):
    conn=connect(tmp_path/"ppa.sqlite"); lid=_library(conn,tmp_path/"lib")
    create_event_from_cluster(conn,library_id=lid,cluster=_cluster("x","2004-12-25",("a","b","c")),name="Christmas")
    view=_view(lid,tuple(_item(x) for x in "abc"))
    assert build_event_home(conn,view).to_json(pretty=False)==build_event_home(conn,view).to_json(pretty=False)


def test_human_cover_overrides_stable_default_but_not_membership(tmp_path):
    from ppa.events import set_event_cover, get_event
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib')
    event=create_event_from_cluster(conn,library_id=lid,cluster=_cluster('x','2004-12-25',('a','b','c')),name='Christmas')
    set_event_cover(conn,event.id,'c')
    card=build_event_home(conn,_view(lid,tuple(_item(x) for x in 'abc'))).card(event.id)
    assert card.cover_file_id == 'c' and card.cover_rule == 'human_preferred_member'
    assert get_event(conn,event.id).file_ids == ('a','b','c')
