from pathlib import Path
from PIL import Image

from ppa import catalogue
from ppa.db import connect
from ppa.organization import create_album, create_tag, bulk_add_photos_to_album, bulk_tag_photos, get_tag
from ppa.organization_suggestions import (
    ORGANIZATION_SUGGESTIONS_SCHEMA, apply_organization_suggestion,
    build_organization_suggestions, build_suggestion_browse,
)
from ppa.scanner import scan_library


def _setup(tmp_path: Path, count: int = 5):
    lib = tmp_path / "library"; lib.mkdir()
    for i in range(count):
        Image.new("RGB", (30, 20), (i * 20, 10, 10)).save(lib / f"p{i}.jpg")
    conn = connect(tmp_path / "db.sqlite3"); scan_library(conn, lib)
    lid = catalogue.list_libraries(conn)[0].id
    pids = tuple(sorted({i.photo_id for i in catalogue.grid_items(conn)}))
    return conn, lid, pids


def _evidence_counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("metadata_observations", "anchors", "reconstructions"))


def test_album_majority_tag_suggestion_is_read_only_and_deterministic(tmp_path: Path):
    conn, lid, pids = _setup(tmp_path)
    album = create_album(conn, library_id=lid, name="Beach Day")
    tag = create_tag(conn, library_id=lid, name="Family")
    bulk_add_photos_to_album(conn, album.id, pids)
    bulk_tag_photos(conn, tag.id, pids[:4])
    before = conn.total_changes
    a = build_organization_suggestions(conn, library_id=lid)
    b = build_organization_suggestions(conn, library_id=lid)
    assert a.schema == ORGANIZATION_SUGGESTIONS_SCHEMA and a.read_only
    assert a.to_json(pretty=False) == b.to_json(pretty=False)
    assert len(a.suggestions) == 1
    s = a.suggestions[0]
    assert s.kind == "album_tag_gap" and s.target_photo_ids == (pids[4],)
    assert s.tagged_count == 4 and s.peer_count == 5 and s.coverage == .8
    assert conn.total_changes == before
    conn.close()


def test_thresholds_are_conservative_and_no_complete_group_suggestion(tmp_path: Path):
    conn, lid, pids = _setup(tmp_path, 6)
    album = create_album(conn, library_id=lid, name="A"); tag = create_tag(conn, library_id=lid, name="T")
    bulk_add_photos_to_album(conn, album.id, pids)
    bulk_tag_photos(conn, tag.id, pids[:4])  # 4/6 < 80%
    assert not build_organization_suggestions(conn, library_id=lid).suggestions
    bulk_tag_photos(conn, tag.id, pids[4:])  # complete -> nothing to suggest
    assert not build_organization_suggestions(conn, library_id=lid).suggestions
    conn.close()


def test_event_support_deduplicates_logical_photo_and_suggests_tag_only(tmp_path: Path):
    conn, lid, pids = _setup(tmp_path)
    # Build a manual event directly with current File identities; Event membership is explicit human state.
    import uuid
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(); eid = str(uuid.uuid4())
    conn.execute("INSERT INTO events(id,library_id,name,start_date,end_date,source_kind,created_at,updated_at) VALUES (?,?,?,?,?,'manual',?,?)",
                 (eid, lid, "Birthday", "2004-01-01", "2004-01-01", now, now))
    for pid in pids:
        fid = conn.execute("SELECT id FROM files WHERE library_id=? AND photo_id=? ORDER BY id LIMIT 1", (lid, pid)).fetchone()[0]
        conn.execute("INSERT INTO event_members(event_id,file_id,role,added_at) VALUES (?,?,'human_added',?)", (eid, fid, now))
    conn.commit()
    tag = create_tag(conn, library_id=lid, name="Family"); bulk_tag_photos(conn, tag.id, pids[:4])
    view = build_organization_suggestions(conn, library_id=lid)
    s = next(x for x in view.suggestions if x.group_kind == "event")
    assert s.group_name == "Birthday" and s.target_photo_ids == (pids[4],)
    assert "Tag 'Family'" in s.rationale
    conn.close()


def test_review_browse_is_logical_photo_and_apply_is_audited(tmp_path: Path):
    conn, lid, pids = _setup(tmp_path)
    album = create_album(conn, library_id=lid, name="A"); tag = create_tag(conn, library_id=lid, name="T")
    bulk_add_photos_to_album(conn, album.id, pids); bulk_tag_photos(conn, tag.id, pids[:4])
    s = build_organization_suggestions(conn, library_id=lid).suggestions[0]
    browse = build_suggestion_browse(conn, s)
    assert browse.total_members == 1 and browse.items[0].photo_id == pids[4]
    apply_organization_suggestion(conn, s)
    assert set(get_tag(conn, tag.id).photo_ids) == set(pids)
    action = conn.execute("SELECT action FROM organization_history WHERE object_kind='tag' AND object_id=? AND photo_id=? ORDER BY id DESC LIMIT 1",
                          (tag.id, pids[4])).fetchone()[0]
    assert action == "add_photo"
    conn.close()


def test_stale_suggestion_fails_closed_without_partial_apply(tmp_path: Path):
    conn, lid, pids = _setup(tmp_path)
    album = create_album(conn, library_id=lid, name="A"); tag = create_tag(conn, library_id=lid, name="T")
    bulk_add_photos_to_album(conn, album.id, pids); bulk_tag_photos(conn, tag.id, pids[:4])
    s = build_organization_suggestions(conn, library_id=lid).suggestions[0]
    # Human curation changed after suggestion review.
    bulk_tag_photos(conn, tag.id, [pids[4]])
    before = set(get_tag(conn, tag.id).photo_ids)
    import pytest
    with pytest.raises(ValueError, match="stale"):
        apply_organization_suggestion(conn, s)
    assert set(get_tag(conn, tag.id).photo_ids) == before
    conn.close()


def test_suggestions_never_touch_evidence_or_source_file(tmp_path: Path):
    conn, lid, pids = _setup(tmp_path)
    album = create_album(conn, library_id=lid, name="Christmas 2004")
    tag = create_tag(conn, library_id=lid, name="25 December 2004")
    bulk_add_photos_to_album(conn, album.id, pids); bulk_tag_photos(conn, tag.id, pids[:4])
    source = Path(conn.execute("SELECT path FROM files WHERE library_id=? ORDER BY id LIMIT 1", (lid,)).fetchone()[0])
    raw = source.read_bytes(); mtime = source.stat().st_mtime_ns; evidence = _evidence_counts(conn)
    view = build_organization_suggestions(conn, library_id=lid)
    assert view.suggestions
    assert _evidence_counts(conn) == evidence
    assert source.read_bytes() == raw and source.stat().st_mtime_ns == mtime
    conn.close()


def test_dismissal_suppresses_only_exact_unchanged_fingerprint(tmp_path: Path):
    from ppa.organization_suggestions import dismiss_organization_suggestion
    conn, lid, pids = _setup(tmp_path, 6)
    album = create_album(conn, library_id=lid, name="A")
    tag = create_tag(conn, library_id=lid, name="T")
    bulk_add_photos_to_album(conn, album.id, pids[:5]); bulk_tag_photos(conn, tag.id, pids[:4])
    s = build_organization_suggestions(conn, library_id=lid).suggestions[0]
    dismiss_organization_suggestion(conn, s, note="Not useful")
    assert not build_organization_suggestions(conn, library_id=lid).suggestions
    # Change the peer/support state while leaving the original candidate present.
    bulk_add_photos_to_album(conn, album.id, [pids[5]])
    bulk_tag_photos(conn, tag.id, [pids[5]])
    fresh = build_organization_suggestions(conn, library_id=lid).suggestions
    assert len(fresh) == 1 and fresh[0].target_photo_ids == s.target_photo_ids
    assert fresh[0].id != s.id
    conn.close()


def test_dismissal_is_audited_and_restore_reenables_exact_suggestion(tmp_path: Path):
    from ppa.organization_suggestions import (
        dismiss_organization_suggestion, list_suggestion_reviews, restore_organization_suggestion,
    )
    conn, lid, pids = _setup(tmp_path)
    album = create_album(conn, library_id=lid, name="A"); tag = create_tag(conn, library_id=lid, name="T")
    bulk_add_photos_to_album(conn, album.id, pids); bulk_tag_photos(conn, tag.id, pids[:4])
    s = build_organization_suggestions(conn, library_id=lid).suggestions[0]
    review = dismiss_organization_suggestion(conn, s, note="Already checked")
    assert review.status == "dismissed" and review.note == "Already checked"
    assert list_suggestion_reviews(conn, library_id=lid, status="dismissed")[0].suggestion_id == s.id
    assert restore_organization_suggestion(conn, library_id=lid, suggestion_id=s.id, note="Revisit")
    assert build_organization_suggestions(conn, library_id=lid).suggestions[0].id == s.id
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM organization_suggestion_review_history WHERE suggestion_id=? ORDER BY id", (s.id,)
    )]
    assert actions == ["dismiss", "restore"]
    conn.close()


def test_stale_suggestion_cannot_be_dismissed(tmp_path: Path):
    import pytest
    from ppa.organization_suggestions import dismiss_organization_suggestion
    conn, lid, pids = _setup(tmp_path)
    album = create_album(conn, library_id=lid, name="A"); tag = create_tag(conn, library_id=lid, name="T")
    bulk_add_photos_to_album(conn, album.id, pids); bulk_tag_photos(conn, tag.id, pids[:4])
    s = build_organization_suggestions(conn, library_id=lid).suggestions[0]
    bulk_tag_photos(conn, tag.id, [pids[4]])
    with pytest.raises(ValueError, match="stale"):
        dismiss_organization_suggestion(conn, s)
    assert not list(conn.execute("SELECT * FROM organization_suggestion_reviews"))
    conn.close()


def test_apply_records_acceptance_history_without_weakening_tag_audit(tmp_path: Path):
    from ppa.organization_suggestions import list_suggestion_reviews
    conn, lid, pids = _setup(tmp_path)
    album = create_album(conn, library_id=lid, name="A"); tag = create_tag(conn, library_id=lid, name="T")
    bulk_add_photos_to_album(conn, album.id, pids); bulk_tag_photos(conn, tag.id, pids[:4])
    s = build_organization_suggestions(conn, library_id=lid).suggestions[0]
    apply_organization_suggestion(conn, s, note="Reviewed in preview")
    reviews = list_suggestion_reviews(conn, library_id=lid, status="accepted")
    assert len(reviews) == 1 and reviews[0].suggestion_id == s.id
    assert reviews[0].note == "Reviewed in preview"
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM organization_history WHERE object_kind='tag' AND object_id=? AND photo_id=?", (tag.id, pids[4])
    )]
    assert actions == ["add_photo"]
    assert conn.execute(
        "SELECT action FROM organization_suggestion_review_history WHERE suggestion_id=?", (s.id,)
    ).fetchone()[0] == "accept"
    conn.close()


def test_review_state_never_changes_evidence_or_source(tmp_path: Path):
    from ppa.organization_suggestions import dismiss_organization_suggestion, restore_organization_suggestion
    conn, lid, pids = _setup(tmp_path)
    album = create_album(conn, library_id=lid, name="Christmas 2004")
    tag = create_tag(conn, library_id=lid, name="25 December 2004")
    bulk_add_photos_to_album(conn, album.id, pids); bulk_tag_photos(conn, tag.id, pids[:4])
    s = build_organization_suggestions(conn, library_id=lid).suggestions[0]
    source = Path(conn.execute("SELECT path FROM files WHERE library_id=? ORDER BY id LIMIT 1", (lid,)).fetchone()[0])
    raw = source.read_bytes(); mtime = source.stat().st_mtime_ns; evidence = _evidence_counts(conn)
    dismiss_organization_suggestion(conn, s)
    restore_organization_suggestion(conn, library_id=lid, suggestion_id=s.id)
    assert _evidence_counts(conn) == evidence
    assert source.read_bytes() == raw and source.stat().st_mtime_ns == mtime
    conn.close()
