from pathlib import Path

import pytest

from ppa.competing_identity import (
    BYTE_IDENTICAL_WHEN_FIRST_OBSERVED, CONVERGED_AFTER_OBSERVED_CHANGE,
    INSUFFICIENT_HISTORY, COMPETING_IDENTITY_INVESTIGATION_SCHEMA,
    investigate_competing_identity,
)
from ppa.db import connect


def _setup(conn, root: Path, specs):
    root.mkdir(parents=True, exist_ok=True)
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root), str(root).casefold()))
    lid = conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?", (str(root).casefold(),)).fetchone()[0]
    for fid,pid,sha in specs:
        if conn.execute("SELECT 1 FROM photos WHERE id=?", (pid,)).fetchone() is None:
            conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'2020-01-01')", (pid,))
        p=root/(fid+'.jpg'); p.write_bytes(fid.encode())
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES (?,?,?,?,1,'2020','2021',?,?,'present','ok')", (fid,pid,str(p),p.name,lid,sha))
    conn.commit(); return lid


def _rev(c,rid,fid,sha,when,sup=None,current=False):
    c.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at,superseded_at) VALUES (?,?,?,1,?,?)",(rid,fid,sha,when,sup))
    if current: c.execute("UPDATE files SET current_revision_id=? WHERE id=?",(rid,fid))


def test_competing_identity_first_observed_same_bytes_is_read_only_candidate(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[('a','p1','same'),('b','p2','same')])
    _rev(c,'ra','a','same','2020',current=True); _rev(c,'rb','b','same','2020',current=True); c.commit()
    before=c.total_changes; v=investigate_competing_identity(c,library_id=lid,sha256='same')
    assert v.schema==COMPETING_IDENTITY_INVESTIGATION_SCHEMA
    assert v.classification==BYTE_IDENTICAL_WHEN_FIRST_OBSERVED
    assert v.photo_ids==('p1','p2')
    assert v.merge_consideration.eligible is True
    assert c.total_changes==before


def test_competing_identity_detects_observed_convergence_without_claiming_correct_owner(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[('a','p1','same'),('b','p2','same')])
    _rev(c,'r1','a','old','2019','2020'); _rev(c,'r2','a','same','2020',current=True); _rev(c,'r3','b','same','2020',current=True); c.commit()
    v=investigate_competing_identity(c,library_id=lid,sha256='same')
    assert v.classification==CONVERGED_AFTER_OBSERVED_CHANGE
    assert next(f for p in v.photos for f in p.files if f.file_id=='a').changed_to_shared_bytes
    assert 'does not decide which logical Photo identity is correct' in v.rationale


def test_competing_identity_withholds_origin_explanation_for_incomplete_history(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[('a','p1','same'),('b','p2','same')])
    _rev(c,'ra','a','same','2020',current=True); c.commit()
    assert investigate_competing_identity(c,library_id=lid,sha256='same').classification==INSUFFICIENT_HISTORY


def test_merge_consideration_fails_closed_on_independent_identity_meaning(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[('a','p1','same'),('b','p2','same')])
    for rid,fid in [('ra','a'),('rb','b')]: _rev(c,rid,fid,'same','2020',current=True)
    c.execute("INSERT INTO tags(id,library_id,name,created_at,updated_at) VALUES ('t',?,'Family','x','x')",(lid,))
    c.execute("INSERT INTO photo_tags(tag_id,photo_id,added_at) VALUES ('t','p2','x')")
    c.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,created_at) VALUES (?,'tag','t','apply','p2','x')",(lid,)); c.commit()
    m=investigate_competing_identity(c,library_id=lid,sha256='same').merge_consideration
    assert not m.eligible and any('Album/Tag' in b for b in m.blockers)


def test_merge_consideration_blocks_other_current_bytes_and_cross_library_scope(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[('a','p1','same'),('b','p2','same'),('c','p1','other')])
    other=tmp_path/'other'; other.mkdir(); c.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(other),str(other).casefold()))
    lid2=c.execute("SELECT id FROM libraries WHERE root_canonical_path=?",(str(other).casefold(),)).fetchone()[0]
    p=other/'d.jpg'; p.write_bytes(b'd'); c.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES ('d','p2',?,'d.jpg',1,'x','x',?,'same','present','ok')",(str(p),lid2)); c.commit()
    m=investigate_competing_identity(c,library_id=lid,sha256='same').merge_consideration
    assert not m.eligible
    assert any('different known current bytes' in b for b in m.blockers)
    assert any('another Library' in b for b in m.blockers)


def test_competing_identity_rejects_non_competing_and_never_touches_authority_or_source(tmp_path):
    c=connect(tmp_path/'p.sqlite'); root=tmp_path/'lib'; lid=_setup(c,root,[('a','p1','same'),('b','p2','other')])
    src=[root/'a.jpg',root/'b.jpg']; sb=[(p.read_bytes(),p.stat().st_mtime_ns) for p in src]
    tables=('metadata_observations','anchors','reconstructions','events','albums','tags','photo_lineage')
    before={t:tuple(tuple(r) for r in c.execute(f'SELECT * FROM {t}')) for t in tables}
    with pytest.raises(ValueError,match='not assigned to multiple'):
        investigate_competing_identity(c,library_id=lid,sha256='same')
    after={t:tuple(tuple(r) for r in c.execute(f'SELECT * FROM {t}')) for t in tables}
    assert before==after and [(p.read_bytes(),p.stat().st_mtime_ns) for p in src]==sb


def test_competing_identity_query_count_is_bounded(tmp_path):
    c=connect(tmp_path/'p.sqlite'); specs=[]
    for i in range(30): specs.append((f'a{i}','p1','same'))
    for i in range(30): specs.append((f'b{i}','p2','same'))
    lid=_setup(c,tmp_path/'lib',specs)
    selects=0
    def trace(sql):
        nonlocal selects
        if sql.lstrip().upper().startswith('SELECT'): selects+=1
    c.set_trace_callback(trace); investigate_competing_identity(c,library_id=lid,sha256='same'); c.set_trace_callback(None)
    assert selects <= 11
