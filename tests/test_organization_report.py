from __future__ import annotations
import json, zipfile
from pathlib import Path

from ppa.db import connect
from ppa.organization import create_album, create_tag, add_photo_to_album, tag_photo
from ppa.organization_report import build_organization_report, export_organization_report_zip
from ppa.organization_views import save_organization_view
from ppa.safe_export import enroll_export_root


def _fixture(tmp_path: Path):
    from ppa.config import Config
    from ppa.scanner import scan_library
    root=tmp_path/'Private Family Photos'; root.mkdir()
    from PIL import Image
    for n in range(3): Image.new('RGB',(8,8),(n*30,0,0)).save(root/f'IMG_{n}.jpg','JPEG')
    cfg=Config(db_path=tmp_path/'secret.sqlite3',log_level='INFO',log_path=tmp_path/'ppa.log',library_directories=[])
    conn=connect(cfg.db_path); scan_library(conn,root)
    lib=conn.execute('SELECT id FROM libraries').fetchone()['id']
    pids=tuple(r['photo_id'] for r in conn.execute('SELECT DISTINCT photo_id FROM files ORDER BY photo_id'))
    return conn,lib,pids,root,cfg


def test_report_is_read_only_and_sanitized(tmp_path):
    conn,lib,pids,root,cfg=_fixture(tmp_path)
    a=create_album(conn,library_id=lib,name='Family',description='Human description')
    t=create_tag(conn,library_id=lib,name='Beach')
    add_photo_to_album(conn,a.id,pids[0]); tag_photo(conn,t.id,pids[0]); save_organization_view(conn,library_id=lib,name='Family Beach',album_ids=[a.id],tag_ids=[t.id])
    before=conn.total_changes; report=build_organization_report(conn,library_id=lib)
    assert conn.total_changes==before and report.read_only
    raw=report.to_json()
    assert 'Family' in raw and 'Beach' in raw
    assert str(root) not in raw and str(cfg.db_path) not in raw
    assert a.id not in raw and t.id not in raw and all(pid not in raw for pid in pids)
    assert 'sha256' not in raw.casefold() and 'cover_path' not in raw
    conn.close()


def test_export_zip_has_only_three_sanitized_files(tmp_path):
    conn,lib,pids,root,cfg=_fixture(tmp_path)
    a=create_album(conn,library_id=lib,name='Holiday'); add_photo_to_album(conn,a.id,pids[0])
    out=tmp_path/'share'/'organisation.zip'; export_organization_report_zip(conn,library_id=lib,output_path=out)
    with zipfile.ZipFile(out) as z:
        assert set(z.namelist())=={'organization-report.json','organization-report.md','README.txt'}
        blob='\n'.join(z.read(n).decode('utf-8') for n in z.namelist())
    assert str(root) not in blob and str(cfg.db_path) not in blob and pids[0] not in blob and a.id not in blob
    assert 'Holiday' in blob
    conn.close()


def test_report_summarises_health_saved_views_and_activity_without_ids(tmp_path):
    conn,lib,pids,root,cfg=_fixture(tmp_path)
    a=create_album(conn,library_id=lib,name='Prints'); t=create_tag(conn,library_id=lib,name='Favourite')
    add_photo_to_album(conn,a.id,pids[0]); tag_photo(conn,t.id,pids[0]); save_organization_view(conn,library_id=lib,name='Print favourites',album_ids=[a.id],tag_ids=[t.id])
    r=build_organization_report(conn,library_id=lib)
    assert r.summary['unorganized_count']==2
    assert r.saved_views==({'name':'Print favourites','albums':['Prints'],'tags':['Favourite']},)
    assert any(x['object']=='Prints' for x in r.recent_activity)
    raw=json.dumps(r.to_dict())
    assert a.id not in raw and t.id not in raw and all(pid not in raw for pid in pids)
    conn.close()


def test_export_does_not_touch_source_bytes_or_mtime(tmp_path):
    conn,lib,pids,root,cfg=_fixture(tmp_path)
    source=next(root.iterdir()); before=source.read_bytes(); mt=source.stat().st_mtime_ns
    enroll_export_root(tmp_path, conn=conn); export_organization_report_zip(conn,library_id=lib,output_path=tmp_path/'out.zip')
    assert source.read_bytes()==before and source.stat().st_mtime_ns==mt
    conn.close()


def test_report_scrubs_identity_embedded_in_human_text_and_activity_prefix(tmp_path):
    conn,lib,pids,root,cfg=_fixture(tmp_path)
    import uuid
    secret_uuid=str(uuid.uuid4())
    secret_hash='a'*64
    secret_path=r'C:\\Users\\someone\\Private Photos\\IMG_0001.jpg'
    a=create_album(conn,library_id=lib,name='Private notes',
                   description=f'Source was {secret_path}; token {secret_uuid}; digest {secret_hash}')
    add_photo_to_album(conn,a.id,pids[0])
    report=build_organization_report(conn,library_id=lib)
    raw=report.to_json()
    assert secret_path not in raw and secret_uuid not in raw and secret_hash not in raw
    assert '<PRIVATE_PATH>' in raw and '<IDENTIFIER>' in raw and '<HASH>' in raw
    # Shareable activity must not leak even a shortened logical Photo identifier.
    assert pids[0][:8] not in raw
    assert any(x['change'] == "Added a photo to Album 'Private notes'" for x in report.recent_activity)
    conn.close()
