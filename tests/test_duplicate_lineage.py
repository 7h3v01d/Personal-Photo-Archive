import sqlite3
from pathlib import Path

import pytest

from ppa.db import connect, current_schema_version
from ppa.duplicate_lineage import (
    DUPLICATE_IDENTITY_SCHEMA, add_lineage, build_duplicate_identity,
    list_lineage, list_lineage_history, remove_lineage,
)


def _library(conn, root: Path, specs):
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)", (str(root), str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?", (str(root).casefold(),)).fetchone()[0]
    for fid,pid,sha,presence in specs:
        if conn.execute("SELECT 1 FROM photos WHERE id=?",(pid,)).fetchone() is None:
            conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",(pid,))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) "
                     "VALUES (?,?,?,?,1,'x','x',?,?,?,'ok')",
                     (fid,pid,str(root/(fid+'.jpg')),fid+'.jpg',lid,sha,presence))
    conn.commit(); return lid


def test_schema_v23_and_exact_duplicate_view_is_existing_identity_only(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib',[
        ('f1','p1','aaa','present'),('f2','p1','aaa','missing'),('f3','p2','bbb','present')])
    assert current_schema_version(conn) >= 23
    before=conn.total_changes
    view=build_duplicate_identity(conn,library_id=lid)
    assert view.schema == DUPLICATE_IDENTITY_SCHEMA
    assert view.duplicate_photos == 1 and view.duplicate_files == 2
    assert view.sets[0].photo_id == 'p1'
    assert [c.file_id for c in view.sets[0].copies] == ['f1','f2']
    assert view.sets[0].present_count == 1
    assert view.divergences == ()
    assert conn.total_changes == before


def test_lineage_connects_distinct_photos_and_is_audited(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); _library(conn,tmp_path/'lib',[
        ('f1','parent','aaa','present'),('f2','child','bbb','present')])
    rel=add_lineage(conn,parent_photo_id='parent',child_photo_id='child',relation_type='edited_variant',note='Colour-corrected copy')
    assert rel.parent_photo_id=='parent' and rel.child_photo_id=='child' and rel.source=='human'
    assert list_lineage(conn,photo_id='parent') == (rel,)
    assert [h.action for h in list_lineage_history(conn,lineage_id=rel.id)] == ['create']
    assert remove_lineage(conn,rel.id) is True
    assert list_lineage(conn,photo_id='parent') == ()
    assert [h.action for h in list_lineage_history(conn,lineage_id=rel.id)] == ['create','remove']


def test_lineage_refuses_self_cycle_and_raw_sql_cycle(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); _library(conn,tmp_path/'lib',[
        ('fa','a','a','present'),('fb','b','b','present'),('fc','c','c','present')])
    with pytest.raises(ValueError,match='own lineage parent'):
        add_lineage(conn,parent_photo_id='a',child_photo_id='a',relation_type='derived_copy')
    add_lineage(conn,parent_photo_id='a',child_photo_id='b',relation_type='derived_copy')
    add_lineage(conn,parent_photo_id='b',child_photo_id='c',relation_type='derived_copy')
    with pytest.raises(sqlite3.IntegrityError,match='cycle'):
        conn.execute("INSERT INTO photo_lineage(id,parent_photo_id,child_photo_id,relation_type,source,created_at) VALUES ('raw','c','a','crop','human','x')")


def test_lineage_refuses_distinct_photo_ids_with_byte_identical_content(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); _library(conn,tmp_path/'lib',[
        ('fa','a','same','present'),('fb','b','same','present')])
    with pytest.raises(ValueError,match='byte-identical content'):
        add_lineage(conn,parent_photo_id='a',child_photo_id='b',relation_type='derived_copy')


def test_duplicate_and_lineage_layer_never_touch_evidence_or_organization(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib',[
        ('fa','a','aa','present'),('fa2','a','aa','present'),('fb','b','bb','present')])
    tables=('metadata_observations','anchors','reconstructions','events','albums','tags')
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in tables}
    build_duplicate_identity(conn,library_id=lid)
    rel=add_lineage(conn,parent_photo_id='a',child_photo_id='b',relation_type='unknown_derivative')
    remove_lineage(conn,rel.id)
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in tables}
    assert before == after


def test_lineage_never_writes_source_files(tmp_path):
    root=tmp_path/'lib'; root.mkdir(); a=root/'a.jpg'; b=root/'b.jpg'
    a.write_bytes(b'parent-bytes'); b.write_bytes(b'child-bytes')
    before=[(p.read_bytes(),p.stat().st_mtime_ns) for p in (a,b)]
    conn=connect(tmp_path/'ppa.sqlite')
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold())); lid=conn.execute('SELECT id FROM libraries').fetchone()[0]
    for fid,pid,p in [('fa','a',a),('fb','b',b)]:
        conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",(pid,))
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256) VALUES (?,?,?,?,?,'x','x',?,?)",
                     (fid,pid,str(p),p.name,p.stat().st_size,lid,fid))
    conn.commit()
    rel=add_lineage(conn,parent_photo_id='a',child_photo_id='b',relation_type='edited_variant')
    remove_lineage(conn,rel.id)
    after=[(p.read_bytes(),p.stat().st_mtime_ns) for p in (a,b)]
    assert after == before


def test_duplicate_view_does_not_call_diverged_current_bytes_exact_duplicates(tmp_path):
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib',[
        ('f1','p1','aaa','present'),('f2','p1','bbb','present'),('f3','p1','aaa','missing')])
    view=build_duplicate_identity(conn,library_id=lid)
    # f1/f3 are still exact current copies; f2 has diverged current bytes.
    assert len(view.sets)==1 and [c.file_id for c in view.sets[0].copies]==['f1','f3']
    assert len(view.divergences)==1
    assert view.divergences[0].photo_id=='p1'
    assert view.divergences[0].known_hashes==('aaa','bbb')


def test_exact_copy_pair_validation_requires_current_hash_not_just_photo_identity(tmp_path):
    from ppa.duplicate_lineage import validate_exact_copy_pair
    conn=connect(tmp_path/'ppa.sqlite')
    lid=_library(conn,tmp_path/'lib',[
        ('f1','p1','aaa','present'),('f2','p1','aaa','present'),('f3','p1','bbb','present'),
        ('f4','p2','aaa','present')])
    pair=validate_exact_copy_pair(conn,library_id=lid,file_ids=('f1','f2'))
    assert {p.file_id for p in pair} == {'f1','f2'}
    with pytest.raises(ValueError,match='not proven current exact copies'):
        validate_exact_copy_pair(conn,library_id=lid,file_ids=('f1','f3'))
    with pytest.raises(ValueError,match='do not share one logical Photo'):
        validate_exact_copy_pair(conn,library_id=lid,file_ids=('f1','f4'))


def _revision(conn, rid, fid, sha, when, *, superseded=None):
    conn.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at,superseded_at) VALUES (?,?,?,1,?,?)",
                 (rid,fid,sha,when,superseded))


def test_divergence_investigation_proves_modified_in_place_from_revision_history(tmp_path):
    from ppa.divergence_investigation import investigate_identity_divergence, MODIFIED_IN_PLACE
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib', [('f1','p','bbb','present'),('f2','p','aaa','present')])
    _revision(conn,'r1','f1','aaa','2020-01-01',superseded='2021-01-01'); _revision(conn,'r2','f1','bbb','2021-01-01')
    _revision(conn,'r3','f2','aaa','2020-01-01'); conn.execute("UPDATE files SET current_revision_id='r2' WHERE id='f1'"); conn.execute("UPDATE files SET current_revision_id='r3' WHERE id='f2'"); conn.commit()
    before=conn.total_changes; view=investigate_identity_divergence(conn,library_id=lid,photo_id='p')
    assert view.classification==MODIFIED_IN_PLACE
    assert next(f for f in view.files if f.file_id=='f1').modified_in_place
    assert conn.total_changes==before


def test_divergence_investigation_distinct_when_first_observed_does_not_claim_derivation(tmp_path):
    from ppa.divergence_investigation import investigate_identity_divergence, DISTINCT_WHEN_FIRST_OBSERVED
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib', [('f1','p','aaa','present'),('f2','p','bbb','present')])
    _revision(conn,'r1','f1','aaa','2020-01-01'); _revision(conn,'r2','f2','bbb','2020-02-01'); conn.execute("UPDATE files SET current_revision_id='r1' WHERE id='f1'"); conn.execute("UPDATE files SET current_revision_id='r2' WHERE id='f2'"); conn.commit()
    view=investigate_identity_divergence(conn,library_id=lid,photo_id='p')
    assert view.classification==DISTINCT_WHEN_FIRST_OBSERVED
    assert 'does not establish derivation' in view.rationale


def test_divergence_investigation_withholds_explanation_when_revision_history_incomplete(tmp_path):
    from ppa.divergence_investigation import investigate_identity_divergence, INSUFFICIENT_EVIDENCE
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib', [('f1','p','aaa','present'),('f2','p','bbb','present')])
    _revision(conn,'r1','f1','aaa','2020-01-01'); conn.execute("UPDATE files SET current_revision_id='r1' WHERE id='f1'"); conn.commit()
    view=investigate_identity_divergence(conn,library_id=lid,photo_id='p')
    assert view.classification==INSUFFICIENT_EVIDENCE


def test_divergence_investigation_rejects_nondivergent_and_never_touches_authority(tmp_path):
    from ppa.divergence_investigation import investigate_identity_divergence
    conn=connect(tmp_path/'ppa.sqlite'); lid=_library(conn,tmp_path/'lib', [('f1','p','aaa','present'),('f2','p','aaa','present')])
    tables=('metadata_observations','anchors','reconstructions','events','albums','tags','photo_lineage')
    before={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in tables}
    with pytest.raises(ValueError,match='does not currently'):
        investigate_identity_divergence(conn,library_id=lid,photo_id='p')
    after={t:tuple(tuple(r) for r in conn.execute(f'SELECT * FROM {t}')) for t in tables}
    assert before==after
