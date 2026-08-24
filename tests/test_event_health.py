from pathlib import Path
from ppa.db import connect
from ppa.event_health import EVENT_HEALTH_SCHEMA, build_event_health, build_event_health_view
from ppa.events import create_event_from_cluster, set_event_cover, update_event_context
from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
from ppa.timeline_clusters import DayCount, TimelineCluster


def _library(conn, root: Path, ids=("a","b","c")):
    root.mkdir(); conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root),str(root).casefold()))
    lid=conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    for fid in ids:
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?, 'x')",(f"p-{fid}",))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id) VALUES (?,?,?,?,1,'x','x',?)",(fid,f"p-{fid}",str(root/f'{fid}.jpg'),f'{fid}.jpg',lid))
    conn.commit(); return lid


def _cluster(ids=("a","b","c")):
    return TimelineCluster("h","day_burst","x","2004-12-25","2004-12-25",len(ids),tuple(ids),(),(DayCount("2004-12-25",len(ids)),),"test")


def _item(fid,lane="placed",stale=False):
    return TimelineItem(fid,f"{fid}.jpg",lane,"reconciled",None if lane=="unplaced" else "2004-12-25",None,
                        "PROBABLY_VALID" if lane=="placed" else "QUESTIONABLE",None,None,None,False,stale,"test")


def _view(lid,items):
    lanes={}
    for lane in ("placed","range","tentative","unplaced"):
        ids=tuple(i.file_id for i in items if i.lane==lane); lanes[lane]=TimelineBucket(lane,len(ids),ids)
    scope=type("Scope",(),{"library_id":lid})(); return TimelineView("ppa-timeline/1","x",True,scope,tuple(items),lanes,())


def test_health_is_read_only_and_complete_means_story_plus_no_attention(tmp_path):
    conn=connect(tmp_path/'p.sqlite'); lid=_library(conn,tmp_path/'lib')
    e=create_event_from_cluster(conn,library_id=lid,cluster=_cluster(),name='Christmas')
    update_event_context(conn,e.id,story_text='Family Christmas lunch')
    view=_view(lid,tuple(_item(x) for x in 'abc'))
    before=conn.total_changes; h=build_event_health(conn,view,e.id)
    assert conn.total_changes==before and h.curation_complete and h.has_story
    assert 'Curation complete' in h.badges and not h.needs_chronology_review


def test_unplaced_or_stale_member_requires_chronology_review(tmp_path):
    conn=connect(tmp_path/'p.sqlite'); lid=_library(conn,tmp_path/'lib')
    e=create_event_from_cluster(conn,library_id=lid,cluster=_cluster(),name='Christmas')
    update_event_context(conn,e.id,description='Family day')
    h=build_event_health(conn,_view(lid,(_item('a'),_item('b','unplaced'),_item('c',stale=True))),e.id)
    assert h.needs_chronology_review and h.contains_unplaced and h.contains_stale and not h.curation_complete
    assert 'Needs chronology review' in h.badges


def test_place_only_context_is_context_but_not_story(tmp_path):
    conn=connect(tmp_path/'p.sqlite'); lid=_library(conn,tmp_path/'lib')
    e=create_event_from_cluster(conn,library_id=lid,cluster=_cluster(),name='Trip')
    update_event_context(conn,e.id,place_text='Sydney')
    h=build_event_health(conn,_view(lid,tuple(_item(x) for x in 'abc')),e.id)
    assert h.has_context and not h.has_story and h.needs_story and not h.curation_complete


def test_custom_cover_is_indicator_not_completion_requirement(tmp_path):
    conn=connect(tmp_path/'p.sqlite'); lid=_library(conn,tmp_path/'lib')
    e=create_event_from_cluster(conn,library_id=lid,cluster=_cluster(),name='Trip')
    update_event_context(conn,e.id,story_text='Story')
    base=build_event_health(conn,_view(lid,tuple(_item(x) for x in 'abc')),e.id)
    set_event_cover(conn,e.id,'b')
    custom=build_event_health(conn,_view(lid,tuple(_item(x) for x in 'abc')),e.id)
    assert base.curation_complete and custom.curation_complete and custom.custom_cover
    assert 'Custom cover' in custom.badges


def test_members_outside_scope_prevent_complete_without_fabricating_chronology(tmp_path):
    conn=connect(tmp_path/'p.sqlite'); lid=_library(conn,tmp_path/'lib')
    e=create_event_from_cluster(conn,library_id=lid,cluster=_cluster(),name='Trip')
    update_event_context(conn,e.id,story_text='Story')
    h=build_event_health(conn,_view(lid,(_item('a'),)),e.id)
    assert h.hidden_members==2 and not h.curation_complete
    assert 'Members outside current Timeline scope' in h.badges
    assert h.needs_attention


def test_health_view_is_deterministic_and_versioned(tmp_path):
    conn=connect(tmp_path/'p.sqlite'); lid=_library(conn,tmp_path/'lib')
    create_event_from_cluster(conn,library_id=lid,cluster=_cluster(),name='Trip')
    view=_view(lid,tuple(_item(x) for x in 'abc'))
    a=build_event_health_view(conn,view); b=build_event_health_view(conn,view)
    assert a.schema==EVENT_HEALTH_SCHEMA and a.read_only and a.to_json(pretty=False)==b.to_json(pretty=False)


def test_hidden_member_is_explicitly_needs_attention(tmp_path):
    conn=connect(tmp_path/'p.sqlite'); lid=_library(conn,tmp_path/'lib')
    e=create_event_from_cluster(conn,library_id=lid,cluster=_cluster(),name='Trip')
    update_event_context(conn,e.id,story_text='Story')
    h=build_event_health(conn,_view(lid,(_item('a'),)),e.id)
    assert h.hidden_members == 2
    assert not h.needs_story
    assert not h.needs_chronology_review
    assert h.needs_attention
    assert not h.curation_complete


def test_health_view_uses_bounded_selects_not_n_plus_one(tmp_path):
    conn=connect(tmp_path/'p.sqlite'); lid=_library(conn,tmp_path/'lib')
    # Reuse the same legitimate member set across many durable Events. The
    # projection cost should remain a bounded set of library-scoped SELECTs.
    import uuid
    for n in range(80):
        eid=str(uuid.uuid4())
        conn.execute(
            "INSERT INTO events(id,library_id,name,start_date,end_date,source_kind,created_at,updated_at) "
            "VALUES (?,?,?,'2004-12-25','2004-12-25','timeline_cluster','x','x')",
            (eid,lid,f'Event {n:03d}'))
        conn.executemany(
            "INSERT INTO event_members(event_id,file_id,role,added_at) VALUES (?,?,'authoritative_seed','x')",
            [(eid,fid) for fid in ('a','b','c')])
    conn.commit()
    view=_view(lid,tuple(_item(x) for x in 'abc'))
    selects=[]
    conn.set_trace_callback(lambda sql: selects.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
    health=build_event_health_view(conn,view)
    conn.set_trace_callback(None)
    assert len(health.events)==80
    assert len(selects) <= 5, selects
