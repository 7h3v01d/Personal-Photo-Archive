from pathlib import Path
import pytest

from ppa.db import connect, current_schema_version
from ppa.identity_resolution import plan_identity_split, execute_identity_split, list_identity_resolutions


def _setup(conn, root: Path, specs):
    root.mkdir(exist_ok=True)
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?",(str(root).casefold(),)).fetchone()[0]
    for fid,pid,sha in specs:
        if conn.execute("SELECT 1 FROM photos WHERE id=?",(pid,)).fetchone() is None:
            conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",(pid,))
        path=root/(fid+'.jpg'); path.write_bytes((fid+'-bytes').encode())
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) "
                     "VALUES (?,?,?,?,1,'x','x',?,?, 'present','ok')",(fid,pid,str(path),path.name,lid,sha))
    conn.commit(); return lid


def test_schema_v24_and_split_moves_complete_hash_cohort_atomically(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_setup(conn,tmp_path/'lib',[
        ('a1','p','aaa'),('a2','p','aaa'),('b1','p','bbb')])
    assert current_schema_version(conn) >= 24
    plan=plan_identity_split(conn,library_id=lid,source_photo_id='p',file_ids=('a2','a1'))
    assert plan.sha256=='aaa' and [f.file_id for f in plan.files]==['a1','a2']
    result=execute_identity_split(conn,plan,note='separate edited cohort')
    assert {r[0] for r in conn.execute("SELECT id FROM files WHERE photo_id=?",(result.new_photo_id,))}=={'a1','a2'}
    assert {r[0] for r in conn.execute("SELECT id FROM files WHERE photo_id='p'")}=={'b1'}
    hist=list_identity_resolutions(conn,photo_id='p')
    assert len(hist)==1 and hist[0]['new_photo_id']==result.new_photo_id and hist[0]['action']=='split_hash_cohort'


def test_split_refuses_partial_same_hash_cohort_and_non_divergent_photo(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_setup(conn,tmp_path/'lib',[
        ('a1','p','aaa'),('a2','p','aaa'),('b1','p','bbb'),('c1','q','ccc'),('c2','q','ccc')])
    with pytest.raises(ValueError,match='complete current-SHA cohort'):
        plan_identity_split(conn,library_id=lid,source_photo_id='p',file_ids=('a1',))
    with pytest.raises(ValueError,match='not currently hash-divergent'):
        plan_identity_split(conn,library_id=lid,source_photo_id='q',file_ids=('c1','c2'))


def test_split_revalidates_under_write_lock_and_refuses_stale_plan(tmp_path):
    db=tmp_path/'ppa.sqlite'; c1=connect(db); lid=_setup(c1,tmp_path/'lib',[
        ('a1','p','aaa'),('b1','p','bbb')])
    plan=plan_identity_split(c1,library_id=lid,source_photo_id='p',file_ids=('a1',))
    c2=connect(db); c2.execute("UPDATE files SET sha256='ccc' WHERE id='b1'"); c2.commit(); c2.close()
    with pytest.raises(ValueError,match='stale'):
        execute_identity_split(c1,plan)
    assert c1.execute("SELECT photo_id FROM files WHERE id='a1'").fetchone()[0]=='p'
    assert c1.execute("SELECT COUNT(*) FROM identity_resolution_history").fetchone()[0]==0


def test_split_refuses_cross_library_hash_cohort(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid1=_setup(conn,tmp_path/'lib1',[('a1','p','aaa'),('b1','p','bbb')])
    lid2=_setup(conn,tmp_path/'lib2',[('a2','p','aaa')])
    assert lid1 != lid2
    with pytest.raises(ValueError,match='spans multiple Libraries'):
        plan_identity_split(conn,library_id=lid1,source_photo_id='p',file_ids=('a1',))


def test_split_is_catalogue_identity_only_and_never_writes_source_or_authority(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); root=tmp_path/'lib'; lid=_setup(conn,root,[('a1','p','aaa'),('b1','p','bbb')])
    files=[root/'a1.jpg',root/'b1.jpg']; source_before=[(p.read_bytes(),p.stat().st_mtime_ns) for p in files]
    tables=('metadata_observations','anchors','reconstructions','events','albums','tags','photo_lineage')
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in tables}
    plan=plan_identity_split(conn,library_id=lid,source_photo_id='p',file_ids=('a1',))
    execute_identity_split(conn,plan)
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in tables}
    assert after==before
    assert [(p.read_bytes(),p.stat().st_mtime_ns) for p in files]==source_before


def test_split_refuses_to_strand_source_organization_membership(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_setup(conn,tmp_path/'lib',[('a1','p','aaa'),('b1','p','bbb')])
    # Put only the bbb copy in another Library, leaving aaa as sole local representation.
    root2=tmp_path/'lib2'; lid2=_setup(conn,root2,[])
    conn.execute("UPDATE files SET library_id=?, path=?, filename='b1.jpg' WHERE id='b1'",(lid2,str(root2/'b1.jpg')))
    conn.execute("INSERT INTO albums(id,library_id,name,created_at,updated_at) VALUES ('al',?,'Keep','x','x')",(lid,))
    conn.execute("INSERT INTO album_photos(album_id,photo_id,added_at) VALUES ('al','p','x')")
    conn.commit()
    # The source is divergent globally, but moving its only File from lid would strand Album membership.
    with pytest.raises(ValueError,match='strand existing Album/Tag membership'):
        plan_identity_split(conn,library_id=lid,source_photo_id='p',file_ids=('a1',))

def test_split_refuses_when_same_hash_already_has_another_logical_photo(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_setup(conn,tmp_path/'lib',[
        ('a1','p','aaa'),('b1','p','bbb'),('other','q','aaa')])
    with pytest.raises(ValueError,match='another logical Photo'):
        plan_identity_split(conn,library_id=lid,source_photo_id='p',file_ids=('a1',))

from ppa.identity_resolution import review_identity_resolution, plan_identity_recovery, execute_identity_recovery


def _make_split(conn, lid):
    plan=plan_identity_split(conn,library_id=lid,source_photo_id='p',file_ids=('a1',))
    return execute_identity_split(conn,plan,note='test split')


def test_schema_v25_and_resolution_review_shows_before_after_topology(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_setup(conn,tmp_path/'lib',[('a1','p','aaa'),('b1','p','bbb')])
    result=_make_split(conn,lid)
    assert current_schema_version(conn) >= 25
    review=review_identity_resolution(conn,result.resolution_id)
    assert review.recovery_eligible is True
    assert review.moved_file_ids==('a1',)
    assert {f.file_id for f in review.source_files_now}=={'b1'}
    assert {f.file_id for f in review.new_photo_files_now}=={'a1'}


def test_recovery_recombines_exact_split_atomically_and_preserves_audit(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_setup(conn,tmp_path/'lib',[('a1','p','aaa'),('b1','p','bbb')])
    split=_make_split(conn,lid); plan=plan_identity_recovery(conn,split.resolution_id)
    result=execute_identity_recovery(conn,plan,note='split was mistaken')
    assert {r[0] for r in conn.execute("SELECT id FROM files WHERE photo_id='p'")}=={'a1','b1'}
    assert conn.execute("SELECT 1 FROM photos WHERE id=?",(split.new_photo_id,)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM identity_resolution_history").fetchone()[0]==1
    row=conn.execute("SELECT * FROM identity_resolution_recovery_history").fetchone()
    assert row['resolution_id']==split.resolution_id and row['action']=='recombine_split'
    assert result.removed_photo_id==split.new_photo_id


def test_recovery_refuses_after_new_photo_organization_even_if_later_removed(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_setup(conn,tmp_path/'lib',[('a1','p','aaa'),('b1','p','bbb')])
    split=_make_split(conn,lid)
    conn.execute("INSERT INTO tags(id,library_id,name,created_at,updated_at) VALUES ('t',?,'Review','x','x')",(lid,))
    conn.execute("INSERT INTO photo_tags(tag_id,photo_id,added_at) VALUES ('t',?,'x')",(split.new_photo_id,))
    conn.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,created_at) VALUES (?,'tag','t','apply',?,'9999-01-01T00:00:00+00:00')",(lid,split.new_photo_id))
    conn.execute("DELETE FROM photo_tags WHERE tag_id='t' AND photo_id=?",(split.new_photo_id,)); conn.commit()
    review=review_identity_resolution(conn,split.resolution_id)
    assert review.recovery_eligible is False and 'curation changed' in review.recovery_reason
    with pytest.raises(ValueError,match='cannot be recombined'):
        plan_identity_recovery(conn,split.resolution_id)


def test_recovery_refuses_if_split_file_bytes_changed_or_cohort_ownership_changed(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_setup(conn,tmp_path/'lib',[('a1','p','aaa'),('b1','p','bbb')])
    split=_make_split(conn,lid)
    conn.execute("UPDATE files SET sha256='ccc' WHERE id='a1'"); conn.commit()
    assert review_identity_resolution(conn,split.resolution_id).recovery_eligible is False


def test_recovery_revalidates_under_write_lock_and_refuses_stale_plan(tmp_path):
    db=tmp_path/'ppa.sqlite'; c1=connect(db); lid=_setup(c1,tmp_path/'lib',[('a1','p','aaa'),('b1','p','bbb')])
    split=_make_split(c1,lid); plan=plan_identity_recovery(c1,split.resolution_id)
    c2=connect(db); c2.execute("UPDATE files SET presence_status='missing' WHERE id='a1'"); c2.commit(); c2.close()
    with pytest.raises(ValueError,match='stale'):
        execute_identity_recovery(c1,plan)
    assert c1.execute("SELECT photo_id FROM files WHERE id='a1'").fetchone()[0]==split.new_photo_id
    assert c1.execute("SELECT COUNT(*) FROM identity_resolution_recovery_history").fetchone()[0]==0


def test_recovery_is_catalogue_identity_only_and_preserves_source_and_authority(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); root=tmp_path/'lib'; lid=_setup(conn,root,[('a1','p','aaa'),('b1','p','bbb')])
    split=_make_split(conn,lid)
    files=[root/'a1.jpg',root/'b1.jpg']; before_src=[(p.read_bytes(),p.stat().st_mtime_ns) for p in files]
    tables=('metadata_observations','anchors','reconstructions','events','albums','tags','photo_lineage')
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in tables}
    execute_identity_recovery(conn,plan_identity_recovery(conn,split.resolution_id))
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in tables}
    assert before==after
    assert [(p.read_bytes(),p.stat().st_mtime_ns) for p in files]==before_src
