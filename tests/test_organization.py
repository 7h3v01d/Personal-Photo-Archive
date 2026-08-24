import sqlite3
from pathlib import Path

import pytest

from ppa.db import connect, current_schema_version
from ppa.organization import (
    add_photo_to_album, create_album, create_tag, get_album, get_tag,
    list_albums, list_organization_history, list_photo_albums, list_photo_tags,
    list_tags, remove_photo_from_album, rename_album, rename_tag, tag_photo,
    untag_photo, update_album_description,
)


def _library(conn, root: Path, specs):
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root), str(root).casefold()))
    lid = conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?", (str(root).casefold(),)).fetchone()[0]
    for fid, pid in specs:
        if conn.execute("SELECT 1 FROM photos WHERE id=?", (pid,)).fetchone() is None:
            conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')", (pid,))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id) "
                     "VALUES (?,?,?,?,1,'x','x',?)", (fid, pid, str(root/(fid+'.jpg')), fid+'.jpg', lid))
    conn.commit(); return lid


def test_schema_v19_and_album_roundtrip_is_logical_photo_based(tmp_path):
    conn = connect(tmp_path/'ppa.sqlite')
    lid = _library(conn, tmp_path/'lib', [('f1','p1'), ('f2','p1'), ('f3','p2')])
    assert current_schema_version(conn) >= 19
    album = create_album(conn, library_id=lid, name='  Family   Favourites ', description='Chosen photos')
    album = add_photo_to_album(conn, album.id, 'p1')
    # A duplicate physical copy of p1 does not create duplicate album membership.
    album = add_photo_to_album(conn, album.id, 'p1')
    assert album.name == 'Family Favourites'
    assert album.photo_ids == ('p1',)
    assert list_albums(conn, library_id=lid)[0].id == album.id


def test_album_and_tag_cross_library_guards_hold_in_api_and_db(tmp_path):
    conn = connect(tmp_path/'ppa.sqlite')
    lid1 = _library(conn, tmp_path/'lib1', [('a','p-a')])
    _library(conn, tmp_path/'lib2', [('b','p-b')])
    album = create_album(conn, library_id=lid1, name='A')
    tag = create_tag(conn, library_id=lid1, name='Family')
    with pytest.raises(ValueError, match='not represented'):
        add_photo_to_album(conn, album.id, 'p-b')
    with pytest.raises(ValueError, match='not represented'):
        tag_photo(conn, tag.id, 'p-b')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO album_photos(album_id,photo_id,added_at) VALUES (?,?,'x')", (album.id,'p-b'))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO photo_tags(tag_id,photo_id,added_at) VALUES (?,?,'x')", (tag.id,'p-b'))


def test_shared_logical_photo_can_be_organized_independently_in_each_library(tmp_path):
    conn = connect(tmp_path/'ppa.sqlite')
    lid1 = _library(conn, tmp_path/'lib1', [('a','shared')])
    lid2 = _library(conn, tmp_path/'lib2', [('b','shared')])
    a1 = create_album(conn, library_id=lid1, name='Library One')
    a2 = create_album(conn, library_id=lid2, name='Library Two')
    add_photo_to_album(conn, a1.id, 'shared')
    add_photo_to_album(conn, a2.id, 'shared')
    assert list_photo_albums(conn, library_id=lid1, photo_id='shared')[0].id == a1.id
    assert list_photo_albums(conn, library_id=lid2, photo_id='shared')[0].id == a2.id


def test_album_curation_is_idempotent_audited_and_reversible(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1'),('b','p2')])
    album=create_album(conn,library_id=lid,name='Trips')
    album=update_album_description(conn,album.id,'Trips we remember')
    album=rename_album(conn,album.id,'Family Trips')
    add_photo_to_album(conn,album.id,'p1'); add_photo_to_album(conn,album.id,'p1')
    remove_photo_from_album(conn,album.id,'p1'); remove_photo_from_album(conn,album.id,'p1')
    assert get_album(conn,album.id).photo_ids == ()
    assert [h.action for h in list_organization_history(conn,object_kind='album',object_id=album.id)] == [
        'create','description','rename','add_photo','remove_photo'
    ]


def test_tags_are_case_insensitive_unique_and_audited(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1')])
    tag=create_tag(conn,library_id=lid,name=' Family ')
    same=create_tag(conn,library_id=lid,name='family')
    assert same.id == tag.id
    tag=rename_tag(conn,tag.id,'Family & Friends')
    tag_photo(conn,tag.id,'p1'); tag_photo(conn,tag.id,'p1')
    assert [t.name for t in list_tags(conn,library_id=lid)] == ['Family & Friends']
    assert list_photo_tags(conn,library_id=lid,photo_id='p1')[0].id == tag.id
    untag_photo(conn,tag.id,'p1'); untag_photo(conn,tag.id,'p1')
    assert get_tag(conn,tag.id).photo_ids == ()
    assert [h.action for h in list_organization_history(conn,object_kind='tag',object_id=tag.id)] == [
        'create','rename','add_photo','remove_photo'
    ]


def test_organization_never_changes_chronology_or_evidence_tables(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1')])
    before = {
        table: tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {table}'))
        for table in ('metadata_observations','anchors','reconstructions')
    }
    album=create_album(conn,library_id=lid,name='Christmas 2004',description='Definitely Christmas Day')
    tag=create_tag(conn,library_id=lid,name='25 December 2004')
    add_photo_to_album(conn,album.id,'p1'); tag_photo(conn,tag.id,'p1')
    after = {
        table: tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {table}'))
        for table in ('metadata_observations','anchors','reconstructions')
    }
    assert before == after


def test_album_and_tag_curation_never_write_source_file(tmp_path):
    import os
    root=tmp_path/'lib'; root.mkdir(); source=root/'photo.jpg'; source.write_bytes(b'original-photo-bytes')
    before_bytes=source.read_bytes(); before_stat=source.stat()
    conn=connect(tmp_path/'ppa.sqlite')
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries").fetchone()[0]
    conn.execute("INSERT INTO photos(id,created_at) VALUES ('p1','x')")
    conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id) "
                 "VALUES ('f1','p1',?,'photo.jpg',?,'x','x',?)",(str(source),len(before_bytes),lid)); conn.commit()
    album=create_album(conn,library_id=lid,name='Family'); tag=create_tag(conn,library_id=lid,name='Favourite')
    add_photo_to_album(conn,album.id,'p1'); tag_photo(conn,tag.id,'p1')
    remove_photo_from_album(conn,album.id,'p1'); untag_photo(conn,tag.id,'p1')
    after_stat=source.stat()
    assert source.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns

from ppa.organization import (
    bulk_add_photos_to_album, bulk_remove_photos_from_album,
    bulk_tag_photos, bulk_untag_photos, get_photo_organization,
)


def test_bulk_album_membership_is_atomic_and_deduplicates_input(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1'),('b','p2')])
    album=create_album(conn,library_id=lid,name='Bulk')
    out=bulk_add_photos_to_album(conn,album.id,['p1','p1','p2'])
    assert out.photo_ids == ('p1','p2')
    assert [h.action for h in list_organization_history(conn,object_kind='album',object_id=album.id)] == ['create','add_photo','add_photo']
    out=bulk_remove_photos_from_album(conn,album.id,['p2','p2'])
    assert out.photo_ids == ('p1',)


def test_bulk_album_fails_all_or_zero_on_cross_library_photo(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib1',[('a','p1')]); _library(conn,tmp_path/'lib2',[('b','p2')])
    album=create_album(conn,library_id=lid,name='Atomic')
    with pytest.raises(ValueError, match='not represented'):
        bulk_add_photos_to_album(conn,album.id,['p1','p2'])
    assert get_album(conn,album.id).photo_ids == ()
    assert [h.action for h in list_organization_history(conn,object_kind='album',object_id=album.id)] == ['create']


def test_bulk_tag_is_atomic_and_photo_organization_is_read_only(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1'),('b','p2')])
    tag=create_tag(conn,library_id=lid,name='People')
    bulk_tag_photos(conn,tag.id,['p1','p2'])
    state=get_photo_organization(conn,library_id=lid,photo_id='p1')
    assert state.tag_ids == (tag.id,)
    before=conn.total_changes
    again=get_photo_organization(conn,library_id=lid,photo_id='p1')
    assert again == state and conn.total_changes == before
    bulk_untag_photos(conn,tag.id,['p1'])
    assert get_photo_organization(conn,library_id=lid,photo_id='p1').tag_ids == ()


def test_bulk_organization_cannot_change_evidence(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1'),('b','p2')])
    album=create_album(conn,library_id=lid,name='Christmas 2004')
    tag=create_tag(conn,library_id=lid,name='25 December 2004')
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    bulk_add_photos_to_album(conn,album.id,['p1','p2']); bulk_tag_photos(conn,tag.id,['p1','p2'])
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    assert before == after

from ppa.organization import (
    get_album_presentation, set_album_cover, set_album_presentation_order,
    reset_album_presentation, list_album_presentation_history,
)


def test_album_presentation_schema_v20_and_cover_order_are_display_only(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1'),('b','p2')])
    assert current_schema_version(conn) >= 20
    album=create_album(conn,library_id=lid,name='Family'); bulk_add_photos_to_album(conn,album.id,['p1','p2'])
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    p=set_album_cover(conn,album.id,'p2'); assert p.cover_photo_id=='p2'
    p=set_album_presentation_order(conn,album.id,['p2','p1']); assert p.order_photo_ids==('p2','p1')
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in ('metadata_observations','anchors','reconstructions')}
    assert before==after
    assert [h.action for h in list_album_presentation_history(conn,album.id)] == ['cover','order']


def test_album_presentation_requires_member_and_exact_permutation(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1'),('b','p2'),('c','p3')])
    album=create_album(conn,library_id=lid,name='A'); bulk_add_photos_to_album(conn,album.id,['p1','p2'])
    with pytest.raises(ValueError, match='current album member'): set_album_cover(conn,album.id,'p3')
    with pytest.raises(ValueError, match='duplicate'): set_album_presentation_order(conn,album.id,['p1','p1'])
    with pytest.raises(ValueError, match='every current album member'): set_album_presentation_order(conn,album.id,['p1'])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO album_presentation(album_id,cover_photo_id,updated_at) VALUES (?,?,?)",(album.id,'p3','x'))


def test_album_membership_change_invalidates_presentation_dependencies(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1'),('b','p2'),('c','p3')])
    album=create_album(conn,library_id=lid,name='A'); bulk_add_photos_to_album(conn,album.id,['p1','p2'])
    set_album_cover(conn,album.id,'p1'); set_album_presentation_order(conn,album.id,['p2','p1'])
    add_photo_to_album(conn,album.id,'p3')
    p=get_album_presentation(conn,album.id); assert p.cover_photo_id=='p1' and p.order_photo_ids is None
    remove_photo_from_album(conn,album.id,'p1')
    p=get_album_presentation(conn,album.id); assert p.cover_photo_id is None and p.order_photo_ids is None


def test_album_presentation_reset_is_audited_and_idempotent(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[('a','p1')])
    album=create_album(conn,library_id=lid,name='A'); add_photo_to_album(conn,album.id,'p1'); set_album_cover(conn,album.id,'p1')
    reset_album_presentation(conn,album.id); reset_album_presentation(conn,album.id)
    assert get_album_presentation(conn,album.id).cover_photo_id is None
    assert [h.action for h in list_album_presentation_history(conn,album.id)] == ['cover','reset']
