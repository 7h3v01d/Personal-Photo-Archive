from pathlib import Path
import pytest
from ppa.db import connect
from ppa.organization import create_tag, tag_photo
from ppa.tag_home import build_tag_home, build_tag_intersection_view, TAG_HOME_SCHEMA


def _library(conn, root: Path, rows):
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root),str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?",(str(root).casefold(),)).fetchone()[0]
    for fid,pid,name,presence in rows:
        if conn.execute("SELECT 1 FROM photos WHERE id=?",(pid,)).fetchone() is None:
            conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",(pid,))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,presence_status,status) VALUES (?,?,?,?,1,'x','x',?,?,?)",(fid,pid,str(root/name),name,lid,presence,'active' if presence=='present' else 'missing'))
    conn.commit(); return lid


def test_tag_home_is_read_only_deterministic_and_one_card_per_tag(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','a.jpg','present'),('f2','p2','b.jpg','missing')])
    t1=create_tag(conn,library_id=lid,name='Family'); t2=create_tag(conn,library_id=lid,name='Beach')
    tag_photo(conn,t1.id,'p1'); tag_photo(conn,t1.id,'p2'); tag_photo(conn,t2.id,'p1')
    before=conn.total_changes; home=build_tag_home(conn,library_id=lid)
    assert home.schema==TAG_HOME_SCHEMA and home.read_only and conn.total_changes==before
    assert [c.name for c in home.cards]==['Beach','Family']
    fam=next(c for c in home.cards if c.name=='Family')
    assert fam.photo_count==2 and fam.present_count==1 and fam.missing_only_count==1
    assert home==build_tag_home(conn,library_id=lid)


def test_tag_home_search_is_name_only_and_case_insensitive(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','Christmas.jpg','present')])
    t=create_tag(conn,library_id=lid,name='Family Beach'); tag_photo(conn,t.id,'p1')
    home=build_tag_home(conn,library_id=lid)
    assert [c.tag_id for c in home.filtered('family beach')]==[t.id]
    assert home.filtered('christmas')==()


def test_tag_intersection_is_explicit_set_intersection_and_logical_photo_unique(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','a.jpg','present'),('f1b','p1','copy.jpg','present'),('f2','p2','b.jpg','present'),('f3','p3','c.jpg','present')])
    family=create_tag(conn,library_id=lid,name='Family'); beach=create_tag(conn,library_id=lid,name='Beach')
    for pid in ('p1','p2'): tag_photo(conn,family.id,pid)
    for pid in ('p1','p3'): tag_photo(conn,beach.id,pid)
    view=build_tag_intersection_view(conn,library_id=lid,tag_ids=(family.id,beach.id))
    assert view.object_kind=='tag_intersection' and view.total_members==1
    assert [i.photo_id for i in view.items]==['p1']


def test_tag_intersection_requires_two_distinct_same_library_tags(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); l1=_library(conn,tmp_path/'a',[('f1','p1','a.jpg','present')]); l2=_library(conn,tmp_path/'b',[('f2','p2','b.jpg','present')])
    a=create_tag(conn,library_id=l1,name='A'); b=create_tag(conn,library_id=l2,name='B')
    with pytest.raises(ValueError): build_tag_intersection_view(conn,library_id=l1,tag_ids=(a.id,))
    with pytest.raises(ValueError): build_tag_intersection_view(conn,library_id=l1,tag_ids=(a.id,b.id))


def test_tag_discovery_never_changes_evidence_tables(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','25-dec-2004.jpg','present')])
    a=create_tag(conn,library_id=lid,name='Christmas 2004'); b=create_tag(conn,library_id=lid,name='25 December 2004'); tag_photo(conn,a.id,'p1'); tag_photo(conn,b.id,'p1')
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    build_tag_home(conn,library_id=lid); build_tag_intersection_view(conn,library_id=lid,tag_ids=(a.id,b.id))
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    assert before==after
