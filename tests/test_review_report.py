from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ppa.activity_runs import ActivityRun, RunEvent
from ppa.pilot_audit import AuditMetric, PilotAuditSnapshot
from ppa.pilot_session import PilotSession
from ppa.review_report import build_review_progress_report, export_review_progress


def snap(*, t="2026-08-23T00:00:00+00:00", root="C:/Secret/Family", usable=1,
         confirmed=0, unresolved=2, stale=0, actionable=2, integrity=0):
    m = lambda n, p: AuditMetric(n, tuple(f"{p}{i}" for i in range(n)))
    return PilotAuditSnapshot(
        "ppa-pilot-audit/1", t, True, 7, root, "2001-private", None, 3,
        m(usable,"u"), m(usable,"r"), m(confirmed,"c"), m(0,"p"), m(stale,"s"),
        m(unresolved,"x"), m(0,"f"), m(actionable,"a"), m(0,"q"), 0,
        {}, {}, {}, {"hash_mismatch": integrity}, 0)


def session(base, final=None):
    import hashlib, json
    sha = lambda s: hashlib.sha256(json.dumps(s.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return PilotSession("ppa-pilot-session/1", "sess-123", base.generated_at,
                        "closed" if final else "open", 7, base.library_root,
                        base.directory_prefix, None, base, sha(base), (),
                        final.generated_at if final else None, final, sha(final) if final else None,
                        None)


def run(run_id, *, library=7, directory="2001-private", started="2026-08-23T00:30:00+00:00"):
    e = RunEvent(started, run_id, "date_review", "start", None, "private path C:/Secret/Family", None,
                 {"library_id": library, "directory": directory, "explicit_files": None})
    end = RunEvent("2026-08-23T00:31:00+00:00", run_id, "date_review", "end", "success", "ready", 60000, None)
    return ActivityRun(run_id, "date_review", started, end.timestamp, "success", 60000, (e,end))


def test_report_contains_deltas_but_no_file_ids_or_raw_scope():
    b = snap()
    cur = snap(t="2026-08-23T01:00:00+00:00", usable=2, confirmed=1, unresolved=1, actionable=1)
    r = build_review_progress_report(session(b), cur, activity_runs=(run("good"),))
    assert next(m for m in r.metrics if m.label == "Usable chronology").delta == 1
    text = r.to_json()
    assert "C:/Secret/Family" not in text
    assert "2001-private" not in text
    assert '"u0"' not in text and '"x0"' not in text
    assert r.operational_runs[0].run_id == "good"


def test_run_scope_and_window_filtering():
    b = snap()
    cur = snap(t="2026-08-23T02:00:00+00:00")
    runs = (run("good"), run("wronglib", library=8), run("wrongdir", directory="other"),
            run("tooearly", started="2026-08-22T23:00:00+00:00"))
    r = build_review_progress_report(session(b), cur, activity_runs=runs)
    assert [x.run_id for x in r.operational_runs] == ["good"]


def test_scope_mismatch_fails_closed():
    b = snap()
    bad = snap(t="2026-08-23T01:00:00+00:00", root="C:/Other")
    with pytest.raises(ValueError, match="scope differs"):
        build_review_progress_report(session(b), bad)


def test_export_is_shareable_and_excludes_private_content(tmp_path):
    b = snap()
    cur = snap(t="2026-08-23T01:00:00+00:00", usable=2, unresolved=1)
    s = session(b)
    class Cfg:
        log_path = tmp_path / "ppa.log"
    # Structured log with private text. The report only takes run summaries, never messages.
    (tmp_path / "ppa.jsonl").write_text(
        '{"timestamp":"2026-08-23T00:30:00+00:00","run_id":"abc","operation":"date_review","run_phase":"start","message":"C:/Secret/Family/photo.jpg","run_detail":{"library_id":7,"directory":"2001-private","explicit_files":null}}\n'
        '{"timestamp":"2026-08-23T00:31:00+00:00","run_id":"abc","operation":"date_review","run_phase":"end","run_outcome":"success","elapsed_ms":60000,"message":"done"}\n', encoding="utf-8")
    out = export_review_progress(Cfg(), s, cur, tmp_path / "share.zip")
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        blob = b"\n".join(zf.read(n) for n in names)
    assert names == {"review-progress.md", "review-progress.json", "README.txt"}
    assert b"C:/Secret/Family" not in blob
    assert b"2001-private" not in blob
    assert b"photo.jpg" not in blob
    assert b"abc" in blob


def test_integrity_attention_is_explicit():
    b = snap()
    cur = snap(t="2026-08-23T01:00:00+00:00", integrity=2)
    r = build_review_progress_report(session(b), cur)
    assert r.current_integrity_flags == 2
    assert r.integrity_status == "attention"
    assert r.report_source_writes == 0
