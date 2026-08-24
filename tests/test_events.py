import sqlite3
from pathlib import Path

import pytest

from ppa.db import connect
from ppa.events import create_event_from_cluster, event_for_cluster, items_for_event, list_events, rename_event, get_event, remove_event_member
from ppa.timeline import TimelineItem, TimelineView, TimelineBucket
from ppa.timeline_clusters import TimelineCluster, DayCount


def _library_and_files(conn, root: Path, ids=("a", "b", "c", "ctx")):
    conn.execute("INSERT INTO libraries (root_display_path, root_canonical_path) VALUES (?, ?)", (str(root), str(root).casefold()))
    lid = conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?", (str(root).casefold(),)).fetchone()[0]
    for fid in ids:
        pid = "p-" + fid
        conn.execute("INSERT INTO photos (id, created_at) VALUES (?, 'x')", (pid,))
        conn.execute("INSERT INTO files (id, photo_id, path, filename, size_bytes, first_seen_at, last_seen_at, library_id) VALUES (?, ?, ?, ?, 1, 'x', 'x', ?)",
                     (fid, pid, str(root / (fid+'.jpg')), fid+'.jpg', lid))
    conn.commit()
    return lid


def _cluster():
    return TimelineCluster("cluster-abc", "day_burst", "2004-12-25 · 3 photos", "2004-12-25", "2004-12-25", 3,
                           ("a", "b", "c"), ("ctx",), (DayCount("2004-12-25", 3),), "test")


def test_event_creation_snapshots_only_authoritative_seed_and_is_stable(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    event=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="  Christmas   2004 ")
    assert event.name == "Christmas 2004"
    assert event.file_ids == ("a","b","c")
    assert "ctx" not in event.file_ids
    assert event_for_cluster(conn, library_id=lid, cluster_key="cluster-abc").id == event.id
    assert list_events(conn, library_id=lid)[0].id == event.id


def test_same_cluster_cannot_silently_create_second_event(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Christmas")
    with pytest.raises(ValueError):
        create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Different name")


def test_cross_library_membership_fails_closed_in_api_and_db(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib1',("a","b")); lid2=_library_and_files(conn,tmp_path/'lib2',("x",))
    bad=TimelineCluster("cluster-bad","day_burst","x","2004-01-01","2004-01-01",2,("a","x"),(),(DayCount("2004-01-01",2),),"x")
    with pytest.raises(ValueError): create_event_from_cluster(conn, library_id=lid, cluster=bad, name="Bad")
    # DB trigger is a second line of defence.
    good=TimelineCluster("cluster-good","day_burst","x","2004-01-01","2004-01-01",2,("a","b"),(),(DayCount("2004-01-01",2),),"x")
    ev=create_event_from_cluster(conn, library_id=lid, cluster=good, name="Good")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO event_members(event_id,file_id,role,added_at) VALUES (?,?,'human_added','x')",(ev.id,"x"))


def test_event_survives_cluster_change_and_membership_does_not_drift(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Christmas")
    changed=TimelineCluster("cluster-new","day_burst","x","2004-12-25","2004-12-25",3,("a","b","ctx"),(),(DayCount("2004-12-25",3),),"x")
    assert event_for_cluster(conn, library_id=lid, cluster_key=changed.key) is None
    assert list_events(conn, library_id=lid)[0].file_ids == ("a","b","c")


def test_rename_changes_interpretation_not_membership(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name="Christmas")
    renamed=rename_event(conn,ev.id,"Family Christmas 2004")
    assert renamed.name == "Family Christmas 2004" and renamed.file_ids == ev.file_ids


def test_items_for_event_preserves_current_timeline_lanes(tmp_path):
    # Event membership is durable, but present-day lane truth is never rewritten.
    event=type('E',(),{'file_ids':('a','b','c')})()
    items=(
        TimelineItem('a','a.jpg','placed','recorded','2004-01-01',None,'PROBABLY_VALID',None,None,None,False,False,'x'),
        TimelineItem('b','b.jpg','unplaced','none',None,None,'QUESTIONABLE',None,None,None,False,False,'x'),
        TimelineItem('c','c.jpg','range','confirmed_reconstruction','2004-01-01','2004-01-03','QUESTIONABLE','CONFIRMED','human_range','confirmed',False,False,'x'),
    )
    lanes={k:TimelineBucket(k,0,()) for k in ('placed','range','tentative','unplaced')}
    scope=type('S',(),{})()
    view=TimelineView('ppa-timeline/1','x',True,scope,items,lanes,())
    assert [i.file_id for i in items_for_event(view,event,lane='unplaced')] == ['b']


def test_event_curation_is_audited_and_does_not_change_chronology(tmp_path):
    from ppa.events import add_event_member, list_event_history, list_event_members, remove_event_member, update_event_note
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib',('a','b','c','ctx','extra'))
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    before_files=tuple(r['id'] for r in conn.execute('SELECT id FROM files ORDER BY id'))
    ev=update_event_note(conn, ev.id, 'At Mum and Dad\'s house')
    ev=add_event_member(conn, ev.id, 'extra')
    ev=remove_event_member(conn, ev.id, 'b')
    ev=rename_event(conn, ev.id, 'Family Christmas 2004')
    assert ev.note == "At Mum and Dad's house"
    assert ev.file_ids == ('a','c','extra')
    assert tuple(r['id'] for r in conn.execute('SELECT id FROM files ORDER BY id')) == before_files
    assert [(m.file_id,m.role) for m in list_event_members(conn,ev.id)] == [('a','authoritative_seed'),('c','authoritative_seed'),('extra','human_added')]
    actions=[h.action for h in list_event_history(conn,ev.id)]
    assert actions == ['create','note','add_member','remove_member','rename']


def test_event_member_add_is_idempotent_and_cross_library_fails(tmp_path):
    from ppa.events import add_event_member, list_event_history
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib1',('a','b','c','ctx','extra'))
    _library_and_files(conn,tmp_path/'lib2',('x',))
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    add_event_member(conn,ev.id,'extra'); add_event_member(conn,ev.id,'extra')
    assert [h.action for h in list_event_history(conn,ev.id)].count('add_member') == 1
    with pytest.raises(ValueError): add_event_member(conn,ev.id,'x')


def test_event_cannot_remove_last_member(tmp_path):
    from ppa.events import remove_event_member
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    remove_event_member(conn,ev.id,'a'); remove_event_member(conn,ev.id,'b')
    with pytest.raises(ValueError, match='retain at least one'):
        remove_event_member(conn,ev.id,'c')


def test_event_history_schema_and_creation_snapshot(tmp_path):
    import json
    from ppa.db import current_schema_version
    from ppa.events import list_event_history
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    assert current_schema_version(conn) >= 14
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    history=list_event_history(conn,ev.id)
    assert len(history) == 1 and history[0].action == 'create'
    snapshot=json.loads(history[0].new_value)
    assert snapshot['file_ids'] == ['a','b','c'] and snapshot['name'] == 'Christmas'


def test_event_context_is_separate_audited_and_does_not_change_membership(tmp_path):
    from ppa.events import get_event, get_event_context, update_event_context, list_event_context_history
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    before=ev.file_ids
    ctx=update_event_context(conn,ev.id,description='Family lunch',place_text="Mum and Dad's house",
                             people_text='Mum, Dad, Leon',occasion_text='Christmas Day',
                             story_text='We opened presents after lunch.')
    assert ctx.place_text == "Mum and Dad's house"
    assert get_event(conn,ev.id).file_ids == before
    hist=list_event_context_history(conn,ev.id)
    assert len(hist) == 1 and 'Family lunch' in hist[0].new_value


def test_event_context_update_is_idempotent_and_versions_real_changes(tmp_path):
    from ppa.events import update_event_context, list_event_context_history
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    update_event_context(conn,ev.id,description='One')
    update_event_context(conn,ev.id,description='One')
    update_event_context(conn,ev.id,description='Two')
    hist=list_event_context_history(conn,ev.id)
    assert len(hist) == 2 and 'One' in hist[1].old_value and 'Two' in hist[1].new_value


def test_event_context_cannot_become_chronology_authority(tmp_path):
    from ppa.events import update_event_context, items_for_event
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    update_event_context(conn,ev.id,place_text='Sydney',story_text='Definitely Christmas Day 1999')
    item=TimelineItem('a','a.jpg','unplaced','none',None,None,'QUESTIONABLE',None,None,None,False,False,'x')
    lanes={k:TimelineBucket(k,0,()) for k in ('placed','range','tentative','unplaced')}
    view=TimelineView('ppa-timeline/1','x',True,type('S',(),{})(),(item,),lanes,())
    assert items_for_event(view,ev)[0].lane == 'unplaced'


def test_event_context_limits_fail_closed(tmp_path):
    from ppa.events import update_event_context
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    with pytest.raises(ValueError, match='place'):
        update_event_context(conn,ev.id,place_text='x'*501)


def test_event_context_schema_v15(tmp_path):
    from ppa.db import current_schema_version
    conn=connect(tmp_path/'ppa.sqlite')
    assert current_schema_version(conn) >= 15


def test_event_presentation_cover_order_are_audited_and_display_only(tmp_path):
    from ppa.events import (get_event_presentation, set_event_cover,
                            set_event_presentation_order, list_event_presentation_history)
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib')
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    set_event_cover(conn, ev.id, 'c')
    set_event_presentation_order(conn, ev.id, ('c','a','b'))
    pref=get_event_presentation(conn,ev.id)
    assert pref.cover_file_id == 'c' and pref.order_file_ids == ('c','a','b')
    assert [h.action for h in list_event_presentation_history(conn,ev.id)] == ['cover','order']
    # Presentation does not change the semantic Event membership snapshot.
    assert get_event(conn,ev.id).file_ids == ('a','b','c')


def test_event_presentation_requires_member_and_exact_permutation(tmp_path):
    from ppa.events import set_event_cover, set_event_presentation_order
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib',('a','b','c','ctx','extra'))
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    with pytest.raises(ValueError, match='current event member'):
        set_event_cover(conn, ev.id, 'extra')
    with pytest.raises(ValueError, match='every current event member'):
        set_event_presentation_order(conn, ev.id, ('a','b'))
    with pytest.raises(ValueError, match='duplicate'):
        set_event_presentation_order(conn, ev.id, ('a','a','c'))


def test_membership_change_invalidates_presentation_dependencies(tmp_path):
    from ppa.events import (add_event_member, get_event_presentation, set_event_cover,
                            set_event_presentation_order)
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library_and_files(conn,tmp_path/'lib',('a','b','c','ctx','extra'))
    ev=create_event_from_cluster(conn, library_id=lid, cluster=_cluster(), name='Christmas')
    set_event_cover(conn,ev.id,'b'); set_event_presentation_order(conn,ev.id,('c','b','a'))
    add_event_member(conn,ev.id,'extra')
    pref=get_event_presentation(conn,ev.id)
    assert pref.cover_file_id == 'b' and pref.order_file_ids is None
    remove_event_member(conn,ev.id,'b')
    pref=get_event_presentation(conn,ev.id)
    assert pref.cover_file_id is None and pref.order_file_ids is None


def test_event_presentation_schema_v16(tmp_path):
    from ppa.db import current_schema_version
    conn=connect(tmp_path/'ppa.sqlite')
    assert current_schema_version(conn) >= 16
