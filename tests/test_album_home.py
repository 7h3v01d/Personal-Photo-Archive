from pathlib import Path

from ppa.album_home import ALBUM_HOME_SCHEMA, build_album_home
from ppa.db import connect
from ppa.organization import create_album, add_photo_to_album, set_album_cover, set_album_presentation_order


def _library(conn, root: Path, rows):
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root), str(root).casefold()))
    lid = conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?", (str(root).casefold(),)).fetchone()[0]
    for fid, pid, filename, presence in rows:
        if conn.execute("SELECT 1 FROM photos WHERE id=?", (pid,)).fetchone() is None:
            conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')", (pid,))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,presence_status,status) "
                     "VALUES (?,?,?,?,1,'x','x',?,?,?)",
                     (fid,pid,str(root/filename),filename,lid,presence,'missing' if presence=='missing' else 'active'))
    conn.commit(); return lid


def test_album_home_is_read_only_searchable_and_deterministic(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','a.jpg','present')])
    a=create_album(conn,library_id=lid,name='Family Trips',description='Beach and camping')
    add_photo_to_album(conn,a.id,'p1'); before=conn.total_changes
    home=build_album_home(conn,library_id=lid)
    assert home.schema == ALBUM_HOME_SCHEMA and home.read_only
    assert home == build_album_home(conn,library_id=lid)
    assert [c.album_id for c in home.filtered('family beach')] == [a.id]
    assert conn.total_changes == before


def test_album_home_cover_prefers_human_choice_over_stable_default(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','a.jpg','present'),('f2','p2','b.jpg','present')])
    a=create_album(conn,library_id=lid,name='A'); add_photo_to_album(conn,a.id,'p1'); add_photo_to_album(conn,a.id,'p2')
    home=build_album_home(conn,library_id=lid); card=home.card(a.id)
    assert card.cover_photo_id == min('p1','p2') and not card.has_custom_cover
    set_album_cover(conn,a.id,'p2')
    card=build_album_home(conn,library_id=lid).card(a.id)
    assert card.cover_photo_id == 'p2' and card.cover_file_id == 'f2' and card.has_custom_cover


def test_album_home_counts_missing_without_dropping_member(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','a.jpg','present'),('f2','p2','b.jpg','missing')])
    a=create_album(conn,library_id=lid,name='Mixed'); add_photo_to_album(conn,a.id,'p1'); add_photo_to_album(conn,a.id,'p2')
    card=build_album_home(conn,library_id=lid).card(a.id)
    assert card.photo_count == 2 and card.present_count == 1 and card.missing_only_count == 1


def test_album_home_default_cover_does_not_follow_custom_order(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','z.jpg','present'),('f2','p2','a.jpg','present')])
    a=create_album(conn,library_id=lid,name='Order'); add_photo_to_album(conn,a.id,'p1'); add_photo_to_album(conn,a.id,'p2')
    first=build_album_home(conn,library_id=lid).card(a.id).cover_photo_id
    set_album_presentation_order(conn,a.id,['p2','p1'])
    card=build_album_home(conn,library_id=lid).card(a.id)
    assert card.cover_photo_id == first and card.has_custom_order


def test_album_home_never_changes_evidence(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('f1','p1','25-dec-2004.jpg','present')])
    a=create_album(conn,library_id=lid,name='Christmas 2004',description='Definitely Christmas Day'); add_photo_to_album(conn,a.id,'p1')
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    build_album_home(conn,library_id=lid).filtered('2004')
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    assert before == after
