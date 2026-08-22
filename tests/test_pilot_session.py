from __future__ import annotations

import json
from pathlib import Path

import pytest

from ppa.pilot_session import (
    SESSION_SCHEMA,
    checkpoint_pilot_session,
    close_pilot_session,
    load_pilot_session,
    save_pilot_session,
    session_from_dict,
    start_pilot_session,
)


def _fake_audit(monkeypatch, *, usable=2, unresolved=1, root="/photos"):
    from ppa.pilot_audit import AuditMetric, PilotAuditSnapshot
    def build(conn, *, library_id, directory_prefix=None, file_ids=None, generated_at=None, **kwargs):
        ids = tuple(file_ids) if file_ids is not None else None
        return PilotAuditSnapshot(
            "ppa-pilot-audit/1", generated_at or "2026-08-23T00:00:00+00:00", True,
            library_id, root, directory_prefix, ids, 3,
            AuditMetric(usable, tuple(f"u{i}" for i in range(usable))),
            AuditMetric(usable, tuple(f"u{i}" for i in range(usable))),
            AuditMetric(0, ()), AuditMetric(0, ()), AuditMetric(0, ()),
            AuditMetric(unresolved, tuple(f"x{i}" for i in range(unresolved))),
            AuditMetric(0, ()), AuditMetric(unresolved, tuple(f"x{i}" for i in range(unresolved))),
            AuditMetric(0, ()), 0, {}, {}, {}, {}, 0,
        )
    monkeypatch.setattr("ppa.pilot_session.build_pilot_audit", build)
    return build


def test_start_save_load_round_trip(monkeypatch, tmp_path):
    _fake_audit(monkeypatch)
    s = start_pilot_session(object(), library_id=7, directory_prefix="2001",
                            session_id="sess", created_at="2026-08-23T00:00:00+00:00")
    assert s.schema == SESSION_SCHEMA and s.status == "open"
    p = tmp_path / "pilot.json"
    save_pilot_session(s, p)
    assert load_pilot_session(p) == s


def test_tampered_baseline_fails_closed(monkeypatch):
    _fake_audit(monkeypatch)
    s = start_pilot_session(object(), library_id=1, session_id="s",
                            created_at="2026-08-23T00:00:00+00:00")
    d = s.to_dict()
    d["baseline"]["total_files"] = 999
    with pytest.raises(ValueError, match="baseline integrity"):
        session_from_dict(d)


def test_checkpoint_is_append_only_and_scope_stable(monkeypatch):
    _fake_audit(monkeypatch)
    s = start_pilot_session(object(), library_id=1, directory_prefix="old", session_id="s",
                            created_at="2026-08-23T00:00:00+00:00")
    s2 = checkpoint_pilot_session(object(), s, label="first",
                                  captured_at="2026-08-23T01:00:00+00:00")
    assert s.checkpoints == ()
    assert len(s2.checkpoints) == 1
    assert s2.checkpoints[0].sequence == 1
    assert s2.baseline_sha256 == s.baseline_sha256


def test_close_preserves_baseline_and_computes_delta(monkeypatch):
    state = {"usable": 1, "unresolved": 2}
    from ppa.pilot_audit import AuditMetric, PilotAuditSnapshot
    def build(conn, *, library_id, directory_prefix=None, file_ids=None, generated_at=None, **kwargs):
        u, x = state["usable"], state["unresolved"]
        return PilotAuditSnapshot(
            "ppa-pilot-audit/1", generated_at or "t", True, library_id, "/photos",
            directory_prefix, tuple(file_ids) if file_ids is not None else None, 3,
            AuditMetric(u, tuple(f"u{i}" for i in range(u))), AuditMetric(u, tuple(f"u{i}" for i in range(u))),
            AuditMetric(0,()), AuditMetric(0,()), AuditMetric(0,()),
            AuditMetric(x, tuple(f"x{i}" for i in range(x))), AuditMetric(0,()),
            AuditMetric(x, tuple(f"x{i}" for i in range(x))), AuditMetric(0,()), 0, {},{},{},{},0)
    monkeypatch.setattr("ppa.pilot_session.build_pilot_audit", build)
    s = start_pilot_session(object(), library_id=1, session_id="s", created_at="start")
    state.update(usable=2, unresolved=1)
    closed = close_pilot_session(object(), s, closed_at="end")
    assert closed.status == "closed"
    assert closed.baseline == s.baseline
    assert closed.comparison.usable_chronology.delta == 1
    assert closed.comparison.unresolved.delta == -1


def test_closed_session_cannot_checkpoint_or_close_again(monkeypatch):
    _fake_audit(monkeypatch)
    s = start_pilot_session(object(), library_id=1, session_id="s", created_at="start")
    closed = close_pilot_session(object(), s, closed_at="end")
    with pytest.raises(ValueError, match="closed"):
        checkpoint_pilot_session(object(), closed)
    with pytest.raises(ValueError, match="already closed"):
        close_pilot_session(object(), closed)


def test_scope_root_change_fails_closed(monkeypatch):
    calls = {"n": 0}
    from ppa.pilot_audit import AuditMetric, PilotAuditSnapshot
    def build(conn, *, library_id, directory_prefix=None, file_ids=None, generated_at=None, **kwargs):
        calls["n"] += 1
        root = "/photos-a" if calls["n"] == 1 else "/photos-b"
        m = AuditMetric(0,())
        return PilotAuditSnapshot("ppa-pilot-audit/1", generated_at or "t", True, library_id, root,
                                  directory_prefix, None, 0, m,m,m,m,m,m,m,m,m,0,{},{},{},{},0)
    monkeypatch.setattr("ppa.pilot_session.build_pilot_audit", build)
    s = start_pilot_session(object(), library_id=1, session_id="s", created_at="start")
    with pytest.raises(ValueError, match="original library/root"):
        checkpoint_pilot_session(object(), s)


def test_atomic_save_leaves_valid_json(monkeypatch, tmp_path):
    _fake_audit(monkeypatch)
    s = start_pilot_session(object(), library_id=1, session_id="s", created_at="start")
    p = tmp_path / "s.json"
    save_pilot_session(s, p)
    parsed = json.loads(p.read_text(encoding="utf-8"))
    assert parsed["session_id"] == "s"
    assert not list(tmp_path.glob("*.tmp"))
