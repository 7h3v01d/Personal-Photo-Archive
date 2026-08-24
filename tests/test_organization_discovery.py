from pathlib import Path
import pytest
from ppa.db import connect
from ppa.organization import create_album, add_photo_to_album, create_tag, tag_photo
from ppa.organization_discovery import build_organization_discovery, ORGANIZATION_DISCOVERY_SCHEMA


def _lib(conn, root: Path, prefix: str, pids):
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?",(str(root).casefold(),)).fetchone()[0]
    for n,pid in enumerate(pids):
        if conn.execute("SELECT 1 FROM photos WHERE id=?",(pid,)).fetchone() is None: conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",(pid,))
        fid=f'{prefix}{n}'; name=f'{prefix}{n}.jpg'
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,presence_status,status) VALUES (?,?,?,?,1,'x','x',?,'present','active')",(fid,pid,str(root/name),name,lid))
    conn.commit(); return lid


def test_combined_album_tag_discovery_is_explicit_intersection(tmp_path):
    c=connect(tmp_path/'db.sqlite'); lid=_lib(c,tmp_path/'l','f',('p1','p2','p3'))
    a=create_album(c,library_id=lid,name='Holidays'); [add_photo_to_album(c,a.id,p) for p in ('p1','p2')]
    t1=create_tag(c,library_id=lid,name='Beach'); [tag_photo(c,t1.id,p) for p in ('p1','p3')]
    t2=create_tag(c,library_id=lid,name='Family'); tag_photo(c,t2.id,'p1')
    r=build_organization_discovery(c,library_id=lid,album_ids=(a.id,),tag_ids=(t1.id,t2.id))
    assert r.schema==ORGANIZATION_DISCOVERY_SCHEMA and r.read_only
    assert r.query.label=='Album: Holidays + Tag: Beach + Tag: Family'
    assert [i.photo_id for i in r.view.items]==['p1']


def test_multiple_albums_are_intersected_not_union(tmp_path):
    c=connect(tmp_path/'db.sqlite'); lid=_lib(c,tmp_path/'l','f',('p1','p2','p3'))
    a=create_album(c,library_id=lid,name='A'); b=create_album(c,library_id=lid,name='B')
    for p in ('p1','p2'): add_photo_to_album(c,a.id,p)
    for p in ('p2','p3'): add_photo_to_album(c,b.id,p)
    r=build_organization_discovery(c,library_id=lid,album_ids=(a.id,b.id))
    assert r.query.photo_ids==('p2',) and r.view.total_members==1


def test_discovery_deduplicates_ids_and_rejects_empty_or_cross_library(tmp_path):
    c=connect(tmp_path/'db.sqlite'); l1=_lib(c,tmp_path/'a','a',('p1',)); l2=_lib(c,tmp_path/'b','b',('p2',))
    a=create_album(c,library_id=l1,name='A'); t=create_tag(c,library_id=l2,name='T')
    with pytest.raises(ValueError): build_organization_discovery(c,library_id=l1)
    with pytest.raises(ValueError): build_organization_discovery(c,library_id=l1,album_ids=(a.id,),tag_ids=(t.id,))
    add_photo_to_album(c,a.id,'p1')
    r=build_organization_discovery(c,library_id=l1,album_ids=(a.id,a.id))
    assert r.query.album_ids==(a.id,) and r.view.total_members==1


def test_discovery_is_logical_photo_unique_across_duplicate_files(tmp_path):
    c=connect(tmp_path/'db.sqlite'); lid=_lib(c,tmp_path/'l','f',('p1','p2'))
    c.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,presence_status,status) VALUES ('dup','p1',?,'copy.jpg',1,'x','x',?,'present','active')",(str(tmp_path/'l'/'copy.jpg'),lid)); c.commit()
    a=create_album(c,library_id=lid,name='A'); t=create_tag(c,library_id=lid,name='T'); add_photo_to_album(c,a.id,'p1'); tag_photo(c,t.id,'p1')
    r=build_organization_discovery(c,library_id=lid,album_ids=(a.id,),tag_ids=(t.id,))
    assert r.view.total_members==1 and len(r.view.items)==1 and r.view.items[0].copy_count==2


def test_discovery_never_changes_evidence(tmp_path):
    c=connect(tmp_path/'db.sqlite'); lid=_lib(c,tmp_path/'l','f',('p1',))
    a=create_album(c,library_id=lid,name='Christmas 2004'); t=create_tag(c,library_id=lid,name='25 December 2004'); add_photo_to_album(c,a.id,'p1'); tag_photo(c,t.id,'p1')
    before={n:tuple(tuple(r) for r in c.execute(f'SELECT * FROM {n}')) for n in ('metadata_observations','anchors','reconstructions')}
    before_changes=c.total_changes; build_organization_discovery(c,library_id=lid,album_ids=(a.id,),tag_ids=(t.id,))
    after={n:tuple(tuple(r) for r in c.execute(f'SELECT * FROM {n}')) for n in before}
    assert before==after and c.total_changes==before_changes
