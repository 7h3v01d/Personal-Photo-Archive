from pathlib import Path
import pytest

from ppa.db import connect, current_schema_version
from ppa.event_home import build_event_home
from ppa.event_search import build_event_search_facets, build_event_search_index, search_event_index
from ppa.event_views import delete_event_view, evaluate_saved_view, list_event_views, save_event_view
from ppa.events import create_event_from_cluster, update_event_context
from ppa.timeline import TimelineBucket, TimelineItem, TimelineView
from ppa.timeline_clusters import DayCount, TimelineCluster


def _setup(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); root=tmp_path/'lib'; root.mkdir()
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=conn.execute('SELECT last_insert_rowid() id').fetchone()['id']
    items=[]
    for fid,day in [('a','2004-12-25'),('b','2004-12-25'),('c','2004-12-25'),('d','2005-02-01'),('e','2005-02-01'),('f','2005-02-01')]:
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",('p-'+fid,))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id) VALUES (?,?,?,?,1,'x','x',?)",(fid,'p-'+fid,str(root/(fid+'.jpg')),fid+'.jpg',lid))
        items.append(TimelineItem(fid,fid+'.jpg','placed','reconciled',day,None,'PROBABLY_VALID',None,None,None,False,False,'test'))
    conn.commit()
    def cluster(key,day,ids): return TimelineCluster(key,'day_burst',day,day,day,len(ids),tuple(ids),(),(DayCount(day,len(ids)),),'test')
    x=create_event_from_cluster(conn,library_id=lid,cluster=cluster('x','2004-12-25',('a','b','c')),name='Christmas Lunch')
    y=create_event_from_cluster(conn,library_id=lid,cluster=cluster('y','2005-02-01',('d','e','f')),name='Sydney Trip')
    update_event_context(conn,x.id,occasion_text='Christmas Day',place_text='Mum and Dad house',people_text='Mum, Dad')
    update_event_context(conn,y.id,occasion_text='Holiday',place_text='Sydney Harbour',people_text='Leon, Maddie')
    lanes={lane:TimelineBucket(lane,len(tuple(i for i in items if i.lane==lane)),tuple(i.file_id for i in items if i.lane==lane)) for lane in ('placed','range','tentative','unplaced')}
    scope=type('Scope',(),{'library_id':lid})(); view=TimelineView('ppa-timeline/1','x',True,scope,tuple(items),lanes,())
    home=build_event_home(conn,view); idx=build_event_search_index(conn,home)
    return conn,lid,idx,x,y


def test_schema_v17_and_saved_view_roundtrip(tmp_path):
    conn,lid,idx,x,y=_setup(tmp_path)
    assert current_schema_version(conn) >= 17
    v=save_event_view(conn,library_id=lid,name='Christmas Events',query_text='christmas',occasion_filter='Christmas')
    loaded=list_event_views(conn,library_id=lid)
    assert loaded==(v,) and evaluate_saved_view(idx,v).hits[0].event_id==x.id


def test_saved_view_is_recipe_not_cached_results(tmp_path):
    conn,lid,idx,x,y=_setup(tmp_path)
    v=save_event_view(conn,library_id=lid,name='Sydney',place_filter='Sydney')
    assert [h.event_id for h in evaluate_saved_view(idx,v).hits]==[y.id]
    update_event_context(conn,x.id,place_text='Sydney suburb')
    home=build_event_home(conn, _setup_view(conn,lid))
    idx2=build_event_search_index(conn,home)
    assert {h.event_id for h in evaluate_saved_view(idx2,v).hits}=={x.id,y.id}


def _setup_view(conn,lid):
    rows=conn.execute('SELECT id,filename FROM files WHERE library_id=? ORDER BY id',(lid,)).fetchall()
    dates={'a':'2004-12-25','b':'2004-12-25','c':'2004-12-25','d':'2005-02-01','e':'2005-02-01','f':'2005-02-01'}
    items=tuple(TimelineItem(r['id'],r['filename'],'placed','reconciled',dates[r['id']],None,'PROBABLY_VALID',None,None,None,False,False,'test') for r in rows)
    lanes={lane:TimelineBucket(lane,len(tuple(i for i in items if i.lane==lane)),tuple(i.file_id for i in items if i.lane==lane)) for lane in ('placed','range','tentative','unplaced')}
    scope=type('Scope',(),{'library_id':lid})(); return TimelineView('ppa-timeline/1','x',True,scope,items,lanes,())


def test_save_same_name_updates_in_place_and_delete(tmp_path):
    conn,lid,idx,*_=_setup(tmp_path)
    a=save_event_view(conn,library_id=lid,name='My View',query_text='christmas')
    b=save_event_view(conn,library_id=lid,name='my view',query_text='sydney',year=2005)
    assert a.id==b.id and len(list_event_views(conn,library_id=lid))==1
    assert evaluate_saved_view(idx,b).hits[0].name=='Sydney Trip'
    assert delete_event_view(conn,b.id) and not list_event_views(conn,library_id=lid)


def test_cross_library_saved_view_application_fails_closed(tmp_path):
    conn,lid,idx,*_=_setup(tmp_path)
    root=tmp_path/'other'; root.mkdir(); conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold())); other=conn.execute('SELECT last_insert_rowid() id').fetchone()['id']; conn.commit()
    v=save_event_view(conn,library_id=other,name='Other')
    with pytest.raises(ValueError,match='library mismatch'): evaluate_saved_view(idx,v)


def test_facets_are_deterministic_and_counts_derive_from_context(tmp_path):
    conn,lid,idx,*_=_setup(tmp_path)
    f=build_event_search_facets(idx)
    assert [(x.value,x.count) for x in f.occasions]==[('Christmas Day',1),('Holiday',1)]
    assert {x.value for x in f.places}=={'Mum and Dad house','Sydney Harbour'}
    assert {x.value for x in f.people}=={'Mum','Dad','Leon','Maddie'}


def test_facet_filters_combine_with_text_and_date(tmp_path):
    conn,lid,idx,x,y=_setup(tmp_path)
    hits=search_event_index(idx,text='trip',place='sydney',person='maddie',start_date='2005-01-01').hits
    assert [h.event_id for h in hits]==[y.id]
    assert hits and search_event_index(idx,text='trip',place='sydney',person='maddie').to_dict()['place']=='sydney'
    assert not search_event_index(idx,text='trip',person='mum').hits
