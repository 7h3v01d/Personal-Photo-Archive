from pathlib import Path

from ppa.db import connect
from ppa.organization import create_album, create_tag, add_photo_to_album, tag_photo
from ppa.organization_browse import build_organization_browse, ORGANIZATION_BROWSE_SCHEMA


def _library(conn, root: Path, rows):
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root), str(root).casefold()))
    lid = conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?", (str(root).casefold(),)).fetchone()[0]
    for fid, pid, filename, presence in rows:
        if conn.execute("SELECT 1 FROM photos WHERE id=?", (pid,)).fetchone() is None:
            conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')", (pid,))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,presence_status,status) "
                     "VALUES (?,?,?,?,1,'x','x',?,?,?)", (fid,pid,str(root/filename),filename,lid,presence,'missing' if presence=='missing' else 'active'))
    conn.commit(); return lid


def test_album_browser_is_read_only_and_one_tile_per_logical_photo(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib', [('f2','p1','z-copy.jpg','present'),('f1','p1','a-copy.jpg','present'),('f3','p2','b.jpg','present')])
    a=create_album(conn,library_id=lid,name='Family'); add_photo_to_album(conn,a.id,'p1'); add_photo_to_album(conn,a.id,'p2')
    before=conn.total_changes
    view=build_organization_browse(conn,object_kind='album',object_id=a.id)
    assert view.schema == ORGANIZATION_BROWSE_SCHEMA and view.read_only
    assert len(view.items)==2 and {i.photo_id for i in view.items}=={'p1','p2'}
    assert next(i for i in view.items if i.photo_id=='p1').file_id == 'f1'
    assert conn.total_changes == before


def test_browser_prefers_present_copy_but_preserves_missing_only_member(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib', [('f1','p1','a.jpg','missing'),('f2','p1','b.jpg','present'),('f3','p2','c.jpg','missing')])
    a=create_album(conn,library_id=lid,name='Mixed'); add_photo_to_album(conn,a.id,'p1'); add_photo_to_album(conn,a.id,'p2')
    view=build_organization_browse(conn,object_kind='album',object_id=a.id)
    assert next(i for i in view.items if i.photo_id=='p1').file_id == 'f2'
    assert next(i for i in view.items if i.photo_id=='p2').status == 'missing'
    assert view.present_members == 1 and view.missing_only_members == 1


def test_tag_browser_and_search_match_any_library_copy_filename(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib', [('f1','p1','representative.jpg','present'),('f2','p1','Grandma-Beach.jpg','present'),('f3','p2','other.jpg','present')])
    t=create_tag(conn,library_id=lid,name='Family'); tag_photo(conn,t.id,'p1'); tag_photo(conn,t.id,'p2')
    view=build_organization_browse(conn,object_kind='tag',object_id=t.id)
    assert [i.photo_id for i in view.filtered('grandma beach')] == ['p1']
    assert len(view.filtered('')) == 2


def test_browse_order_is_deterministic_and_not_chronology_driven(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib', [('f1','p1','z.jpg','present'),('f2','p2','A.jpg','present')])
    a=create_album(conn,library_id=lid,name='Order'); add_photo_to_album(conn,a.id,'p1'); add_photo_to_album(conn,a.id,'p2')
    first=build_organization_browse(conn,object_kind='album',object_id=a.id)
    second=build_organization_browse(conn,object_kind='album',object_id=a.id)
    assert [i.photo_id for i in first.items] == ['p2','p1']
    assert first == second


def test_browse_never_changes_evidence_tables(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','25-dec-2004.jpg','present')])
    a=create_album(conn,library_id=lid,name='Christmas 2004'); add_photo_to_album(conn,a.id,'p1')
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    build_organization_browse(conn,object_kind='album',object_id=a.id).filtered('2004')
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    assert before == after

from ppa.organization import set_album_cover, set_album_presentation_order


def test_album_browser_honours_custom_order_without_chronology_semantics(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib',[('f1','p1','A.jpg','present'),('f2','p2','B.jpg','present')])
    a=create_album(conn,library_id=lid,name='Order'); add_photo_to_album(conn,a.id,'p1'); add_photo_to_album(conn,a.id,'p2')
    set_album_presentation_order(conn,a.id,['p2','p1'])
    view=build_organization_browse(conn,object_kind='album',object_id=a.id)
    assert [i.photo_id for i in view.items]==['p2','p1']


def test_album_cover_uses_logical_photo_identity_not_representative_file(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib',[('f2','p1','z.jpg','present'),('f1','p1','a.jpg','present')])
    a=create_album(conn,library_id=lid,name='Cover'); add_photo_to_album(conn,a.id,'p1'); set_album_cover(conn,a.id,'p1')
    view=build_organization_browse(conn,object_kind='album',object_id=a.id)
    assert view.items[0].file_id == 'f1'
