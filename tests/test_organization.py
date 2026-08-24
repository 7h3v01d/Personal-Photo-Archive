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
