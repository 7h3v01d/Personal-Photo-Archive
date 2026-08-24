from ppa.db.connection import connect
from ppa.event_activity import (continue_event_id, get_event_activity, list_favorite_event_ids,
                                list_recent_event_ids, record_event_view, set_event_favorite)


def _fixture(tmp_path):
    conn = connect(tmp_path / "x.sqlite")
    root = tmp_path / "photos"; root.mkdir()
    cur = conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path,state) VALUES (?,?, 'active')", (str(root), str(root).casefold()))
    lid = cur.lastrowid
    ids=[]
    for n in range(3):
        eid=f"e{n}"
        conn.execute("INSERT INTO events(id,library_id,name,start_date,end_date,source_kind,created_at,updated_at) VALUES (?,?,?,'2004-01-01','2004-01-01','manual', 'x','x')", (eid,lid,eid))
        ids.append(eid)
    conn.commit(); return conn,lid,ids


def test_favorite_is_durable_navigation_only(tmp_path):
    conn,lid,ids=_fixture(tmp_path)
    assert list_favorite_event_ids(conn, library_id=lid)==()
    a=set_event_favorite(conn, ids[1], True)
    assert a.favorite and a.view_count==0
    assert list_favorite_event_ids(conn, library_id=lid)==(ids[1],)
    set_event_favorite(conn, ids[1], False)
    assert list_favorite_event_ids(conn, library_id=lid)==()


def test_recent_and_continue_are_deterministic(tmp_path):
    conn,lid,ids=_fixture(tmp_path)
    record_event_view(conn,ids[0],viewed_at="2026-01-01T00:00:00+00:00")
    record_event_view(conn,ids[1],viewed_at="2026-01-02T00:00:00+00:00")
    assert list_recent_event_ids(conn, library_id=lid)==(ids[1],ids[0])
    assert continue_event_id(conn, library_id=lid)==ids[1]
    assert get_event_activity(conn,ids[1]).view_count==1


def test_recent_retention_is_bounded_but_favorite_survives(tmp_path):
    conn,lid,ids=_fixture(tmp_path)
    set_event_favorite(conn,ids[0],True)
    for n,eid in enumerate(ids):
        record_event_view(conn,eid,viewed_at=f"2026-01-0{n+1}T00:00:00+00:00",recent_limit=2)
    assert list_recent_event_ids(conn, library_id=lid, limit=10)==(ids[2],ids[1])
    assert get_event_activity(conn,ids[0]).last_viewed_at is None
    assert get_event_activity(conn,ids[0]).favorite


def test_cross_library_state_rejected_by_trigger(tmp_path):
    conn,lid,ids=_fixture(tmp_path)
    root=tmp_path/'other';root.mkdir()
    other=conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path,state) VALUES (?,?, 'active')",(str(root),str(root).casefold())).lastrowid
    conn.commit()
    try:
        conn.execute("INSERT INTO event_navigation_state(event_id,library_id,favorite,view_count,updated_at) VALUES (?,?,1,0,'x')",(ids[0],other))
        conn.commit(); assert False
    except Exception:
        conn.rollback()
