from pathlib import Path
from io import BytesIO
import hashlib
from PIL import Image
from ppa.db import connect
from ppa.identity_health import IDENTITY_HEALTH_SCHEMA, build_identity_health
from ppa.identity_resolution import plan_identity_split, execute_identity_split, plan_identity_recovery, execute_identity_recovery


def _bytes_for(label: str) -> bytes:
    digest=hashlib.sha256(label.encode()).digest()
    out=BytesIO()
    Image.new("RGB",(12,10),color=(digest[0],digest[1],digest[2])).save(out,format="JPEG")
    return out.getvalue()


def _setup(conn, root: Path, specs):
    root.mkdir(parents=True,exist_ok=True)
    conn.execute("INSERT INTO libraries(root_display_path,root_canonical_path) VALUES (?,?)",(str(root),str(root).casefold()))
    lid=conn.execute("SELECT id FROM libraries WHERE root_canonical_path=?",(str(root).casefold(),)).fetchone()[0]
    for fid,pid,label in specs:
        if conn.execute("SELECT 1 FROM photos WHERE id=?",(pid,)).fetchone() is None:
            conn.execute("INSERT INTO photos(id,created_at) VALUES (?,'x')",(pid,))
        data=_bytes_for(label); sha=hashlib.sha256(data).hexdigest(); p=root/(fid+'.jpg'); p.write_bytes(data)
        conn.execute("INSERT INTO files(id,photo_id,path,filename,size_bytes,first_seen_at,last_seen_at,library_id,sha256,presence_status,health_status) VALUES (?,?,?,?,?,'x','x',?,?,'present','ok')",(fid,pid,str(p),p.name,len(data),lid,sha))
        rid='r-'+fid
        conn.execute("INSERT INTO file_revisions(id,file_id,sha256,size_bytes,first_observed_at) VALUES (?,?,?,?,'x')",(rid,fid,sha,len(data)))
        conn.execute("UPDATE files SET current_revision_id=? WHERE id=?",(rid,fid))
    conn.commit(); return lid


def test_identity_health_prioritises_competing_identity_before_divergence(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[
        ('a','p1','same'),('b','p2','same'),('c','p3','x'),('d','p3','y')])
    before=c.total_changes; v=build_identity_health(c,library_id=lid)
    assert v.schema==IDENTITY_HEALTH_SCHEMA
    assert [i.kind for i in v.items[:2]]==['competing_identity','identity_divergence']
    assert v.competing_identity_count==1 and v.divergence_count==1
    assert c.total_changes==before


def test_identity_health_marks_fresh_split_recoverable(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[('a','p','a'),('b','p','b')])
    s=execute_identity_split(c,plan_identity_split(c,library_id=lid,source_photo_id='p',file_ids=('a',)))
    v=build_identity_health(c,library_id=lid)
    item=next(i for i in v.items if i.resolution_id==s.resolution_id)
    assert item.kind=='recoverable_split' and item.status=='actionable'


def test_identity_health_marks_historically_curated_split_review_only(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[('a','p','a'),('b','p','b')])
    s=execute_identity_split(c,plan_identity_split(c,library_id=lid,source_photo_id='p',file_ids=('a',)))
    c.execute("INSERT INTO tags(id,library_id,name,created_at,updated_at) VALUES ('t',?,'X','x','x')",(lid,))
    c.execute("INSERT INTO organization_history(library_id,object_kind,object_id,action,photo_id,created_at) VALUES (?,'tag','t','apply',?,'9999-01-01T00:00:00+00:00')",(lid,s.new_photo_id)); c.commit()
    item=next(i for i in build_identity_health(c,library_id=lid).items if i.resolution_id==s.resolution_id)
    assert item.kind=='review_only_split' and 'curation changed' in item.reason


def test_identity_health_retains_recombined_split_as_info(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[('a','p','a'),('b','p','b')])
    s=execute_identity_split(c,plan_identity_split(c,library_id=lid,source_photo_id='p',file_ids=('a',)))
    execute_identity_recovery(c,plan_identity_recovery(c,s.resolution_id))
    item=next(i for i in build_identity_health(c,library_id=lid).items if i.resolution_id==s.resolution_id)
    assert item.kind=='recombined_split' and item.status=='complete'


def test_identity_health_never_changes_authority_or_source(tmp_path):
    c=connect(tmp_path/'p.sqlite'); root=tmp_path/'lib'; lid=_setup(c,root,[('a','p','a'),('b','p','b')])
    src=[root/'a.jpg',root/'b.jpg']; sb=[(p.read_bytes(),p.stat().st_mtime_ns) for p in src]
    tables=('metadata_observations','anchors','reconstructions','events','albums','tags','photo_lineage')
    before={t:tuple(tuple(r) for r in c.execute(f'SELECT * FROM {t}')) for t in tables}
    build_identity_health(c,library_id=lid)
    after={t:tuple(tuple(r) for r in c.execute(f'SELECT * FROM {t}')) for t in tables}
    assert before==after and [(p.read_bytes(),p.stat().st_mtime_ns) for p in src]==sb


def test_identity_health_query_count_is_bounded(tmp_path):
    c=connect(tmp_path/'p.sqlite'); lid=_setup(c,tmp_path/'lib',[(f'a{i}',f'p{i}','same' if i<20 else str(i)) for i in range(60)])
    selects=0
    def trace(sql):
        nonlocal selects
        if sql.lstrip().upper().startswith('SELECT'): selects+=1
    c.set_trace_callback(trace); build_identity_health(c,library_id=lid); c.set_trace_callback(None)
    assert selects <= 8
