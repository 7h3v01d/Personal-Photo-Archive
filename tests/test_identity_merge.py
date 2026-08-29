from pathlib import Path
from io import BytesIO
import hashlib
import pytest
from PIL import Image

from ppa.competing_identity import investigate_competing_identity
from ppa.db import connect
from ppa.identity_merge import (IDENTITY_MERGE_PLAN_SCHEMA, plan_identity_merge,
                                execute_identity_merge, list_identity_merges)


def _image_bytes() -> bytes:
    out = BytesIO()
    Image.new("RGB", (16, 12), color="red").save(out, format="JPEG")
    return out.getvalue()


_SHARED_BYTES = _image_bytes()
_SHARED_SHA = hashlib.sha256(_SHARED_BYTES).hexdigest()


def _setup(tmp_path: Path, *, files=(('a','p1'),('b','p2'))):
    c=connect(tmp_path/'p.sqlite'); root=tmp_path/'lib'; root.mkdir()
    c.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=c.execute("SELECT id FROM libraries").fetchone()[0]
    for fid,pid in files:
        if c.execute("SELECT 1 FROM photos WHERE id=?",(pid,)).fetchone() is None:
            c.execute("INSERT INTO photos(id,created_at) VALUES (?,'2020')",(pid,))
        path=root/(fid+'.jpg'); path.write_bytes(_SHARED_BYTES)
        c.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES (?,?,?,?,?,'2020','2021',?,?,'present','ok')",(fid,pid,str(path),path.name,len(_SHARED_BYTES),lid,_SHARED_SHA))
        rid='r'+fid
        c.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES (?,?,?,?,'2020')",(rid,fid,_SHARED_SHA,len(_SHARED_BYTES)))
        c.execute("UPDATE files SET current_revision_id=? WHERE id=?",(rid,fid))
    c.commit(); return c,lid,root


def test_schema_v26_and_merge_plan_requires_explicit_survivor(tmp_path):
    c,lid,_=_setup(tmp_path)
    versions={r[0] for r in c.execute('SELECT version FROM schema_version')}
    assert 26 in versions
    p=plan_identity_merge(c,library_id=lid,sha256=_SHARED_SHA,survivor_photo_id='p1')
    assert p.schema==IDENTITY_MERGE_PLAN_SCHEMA and p.retired_photo_id=='p2' and p.moved_file_ids==('b',)
    with pytest.raises(ValueError,match='survivor'):
        plan_identity_merge(c,library_id=lid,sha256=_SHARED_SHA,survivor_photo_id='not-a-photo')


def test_controlled_merge_reassigns_files_retires_only_loser_and_audits(tmp_path):
    c,lid,_=_setup(tmp_path,files=(('a','p1'),('b','p2'),('c','p2')))
    plan=plan_identity_merge(c,library_id=lid,sha256=_SHARED_SHA,survivor_photo_id='p1')
    result=execute_identity_merge(c,plan,note='reviewed')
    assert result.survivor_photo_id=='p1' and result.retired_photo_id=='p2'
    assert {r[0] for r in c.execute("SELECT id FROM files WHERE photo_id='p1'")}=={'a','b','c'}
    assert c.execute("SELECT 1 FROM photos WHERE id='p2'").fetchone() is None
    row=list_identity_merges(c)[0]
    assert row['merge_id']==result.merge_id and row['note']=='reviewed' and row['action']=='merge_competing_identity'


def test_merge_plan_stales_if_file_state_changes_before_write_lock(tmp_path):
    c,lid,_=_setup(tmp_path)
    plan=plan_identity_merge(c,library_id=lid,sha256=_SHARED_SHA,survivor_photo_id='p1')
    c.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES ('changed','b',?,?,'2022')",(_SHARED_SHA,len(_SHARED_BYTES))); c.execute("UPDATE files SET current_revision_id='changed' WHERE id='b'"); c.commit()
    with pytest.raises(ValueError,match='stale'):
        execute_identity_merge(c,plan)
    assert c.execute("SELECT photo_id FROM files WHERE id='b'").fetchone()[0]=='p2'
    assert c.execute("SELECT 1 FROM photos WHERE id='p2'").fetchone() is not None
    assert not list_identity_merges(c)


def test_merge_refuses_independent_photo_notes_and_organization_history(tmp_path):
    c,lid,_=_setup(tmp_path)
    c.execute("UPDATE photos SET notes='This identity has its own meaning' WHERE id='p2'"); c.commit()
    inv=investigate_competing_identity(c,library_id=lid,sha256=_SHARED_SHA)
    assert not inv.merge_consideration.eligible and any('notes' in b for b in inv.merge_consideration.blockers)
    with pytest.raises(ValueError,match='not eligible'):
        plan_identity_merge(c,library_id=lid,sha256=_SHARED_SHA,survivor_photo_id='p1')


def test_merge_refuses_prior_identity_history_and_does_not_chain_silently(tmp_path):
    c,lid,_=_setup(tmp_path)
    plan=plan_identity_merge(c,library_id=lid,sha256=_SHARED_SHA,survivor_photo_id='p1')
    execute_identity_merge(c,plan)
    # Introduce a third duplicate identity after the audited merge. The survivor
    # now has merge history, so another automatic merge consideration is blocked.
    root=tmp_path/'lib'; p=root/'d.jpg'; p.write_bytes(_SHARED_BYTES)
    c.execute("INSERT INTO photos(id,created_at) VALUES ('p3','2022')")
    c.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES ('d','p3',?,'d.jpg',?,'2022','2022',?,?,'present','ok')",(str(p),len(_SHARED_BYTES),lid,_SHARED_SHA))
    c.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES ('rd','d',?,?,'2022')",(_SHARED_SHA,len(_SHARED_BYTES)))
    c.execute("UPDATE files SET current_revision_id='rd' WHERE id='d'"); c.commit()
    inv=investigate_competing_identity(c,library_id=lid,sha256=_SHARED_SHA)
    assert not inv.merge_consideration.eligible and any('merge history' in b for b in inv.merge_consideration.blockers)


def test_merge_never_changes_evidence_or_source_bytes(tmp_path):
    c,lid,root=_setup(tmp_path)
    src=[root/'a.jpg',root/'b.jpg']; source_before=[(p.read_bytes(),p.stat().st_mtime_ns) for p in src]
    tables=('metadata_observations','anchors','reconstructions','events','albums','tags','photo_lineage','file_revisions')
    before={t:tuple(tuple(r) for r in c.execute(f'SELECT * FROM {t} ORDER BY rowid')) for t in tables}
    execute_identity_merge(c,plan_identity_merge(c,library_id=lid,sha256=_SHARED_SHA,survivor_photo_id='p1'))
    after={t:tuple(tuple(r) for r in c.execute(f'SELECT * FROM {t} ORDER BY rowid')) for t in tables}
    assert before==after
    assert source_before==[(p.read_bytes(),p.stat().st_mtime_ns) for p in src]


def test_merge_is_atomic_if_retired_photo_cannot_be_deleted(tmp_path):
    c,lid,_=_setup(tmp_path)
    plan=plan_identity_merge(c,library_id=lid,sha256=_SHARED_SHA,survivor_photo_id='p1')
    # Simulate an unforeseen dependent reference not represented by eligibility.
    c.execute("CREATE TABLE blocker(photo_id TEXT REFERENCES photos(id) ON DELETE RESTRICT)")
    c.execute("INSERT INTO blocker(photo_id) VALUES ('p2')"); c.commit()
    with pytest.raises(Exception):
        execute_identity_merge(c,plan)
    assert c.execute("SELECT photo_id FROM files WHERE id='b'").fetchone()[0]=='p2'
    assert c.execute("SELECT 1 FROM photos WHERE id='p2'").fetchone() is not None
    assert not list_identity_merges(c)
