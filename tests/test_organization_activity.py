from pathlib import Path
import pytest

from ppa.db import connect
from ppa.organization import (create_album, create_tag, add_photo_to_album, remove_photo_from_album,
                              tag_photo, untag_photo, get_album, get_tag)
from ppa.organization_activity import build_organization_activity, undo_organization_membership


def _setup(tmp_path: Path):
    conn=connect(tmp_path/'ppa.sqlite'); root=tmp_path/'lib'
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lib=conn.execute("SELECT id FROM libraries ORDER BY id DESC LIMIT 1").fetchone()[0]
    for n in range(3):
        pid=f'p{n}'; fid=f'f{n}'
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",(pid,))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id) VALUES (?,?,?,?,1,'x','x',?)",(fid,pid,str(root/(fid+'.jpg')),fid+'.jpg',lib))
    conn.commit(); return conn,lib,('p0','p1','p2')


def test_activity_is_read_only_human_readable_and_recent(tmp_path):
    conn,lib,pids=_setup(tmp_path); album=create_album(conn,library_id=lib,name='Family'); add_photo_to_album(conn,album.id,pids[0])
    before=conn.total_changes; view=build_organization_activity(conn,library_id=lib)
    assert conn.total_changes==before and view.read_only and view.schema=='ppa-organization-activity/1'
    assert view.entries[0].summary.startswith('Added Photo') and view.entries[0].undoable


def test_undo_album_add_is_atomic_audited_and_single_use(tmp_path):
    conn,lib,pids=_setup(tmp_path); album=create_album(conn,library_id=lib,name='A'); add_photo_to_album(conn,album.id,pids[0])
    entry=build_organization_activity(conn,library_id=lib).entries[0]
    inverse=undo_organization_membership(conn,library_id=lib,history_id=entry.id)
    assert pids[0] not in get_album(conn,album.id).photo_ids and inverse.action=='undo_add_photo'
    with pytest.raises(ValueError,match='cannot be safely undone'):
        undo_organization_membership(conn,library_id=lib,history_id=entry.id)


def test_undo_remove_restores_membership(tmp_path):
    conn,lib,pids=_setup(tmp_path); tag=create_tag(conn,library_id=lib,name='Family'); tag_photo(conn,tag.id,pids[0]); untag_photo(conn,tag.id,pids[0])
    entry=build_organization_activity(conn,library_id=lib).entries[0]
    undo_organization_membership(conn,library_id=lib,history_id=entry.id)
    assert pids[0] in get_tag(conn,tag.id).photo_ids


def test_older_membership_action_refuses_when_pair_changed(tmp_path):
    conn,lib,pids=_setup(tmp_path); album=create_album(conn,library_id=lib,name='A'); add_photo_to_album(conn,album.id,pids[0])
    first=build_organization_activity(conn,library_id=lib).entries[0]; remove_photo_from_album(conn,album.id,pids[0])
    assert not next(e for e in build_organization_activity(conn,library_id=lib).entries if e.id==first.id).undoable
    with pytest.raises(ValueError,match='cannot be safely undone'):
        undo_organization_membership(conn,library_id=lib,history_id=first.id)


def test_rename_visible_but_not_automatically_undoable(tmp_path):
    from ppa.organization import rename_album
    conn,lib,_=_setup(tmp_path); album=create_album(conn,library_id=lib,name='A'); rename_album(conn,album.id,'B')
    e=build_organization_activity(conn,library_id=lib).entries[0]
    assert e.action=='rename' and not e.undoable and 'Renamed Album' in e.summary


def test_activity_and_undo_do_not_touch_evidence(tmp_path):
    conn,lib,pids=_setup(tmp_path); album=create_album(conn,library_id=lib,name='Christmas 2004')
    snapshot=tuple((t,conn.execute(f'SELECT COUNT(*) AS n FROM {t}').fetchone()['n']) for t in ('metadata_observations','anchors','reconstructions'))
    add_photo_to_album(conn,album.id,pids[0]); entry=build_organization_activity(conn,library_id=lib).entries[0]; undo_organization_membership(conn,library_id=lib,history_id=entry.id)
    after=tuple((t,conn.execute(f'SELECT COUNT(*) AS n FROM {t}').fetchone()['n']) for t in ('metadata_observations','anchors','reconstructions'))
    assert after==snapshot
