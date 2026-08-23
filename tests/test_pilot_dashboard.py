from __future__ import annotations

from ppa.pilot_audit import AuditMetric, PilotAuditSnapshot
from ppa.pilot_dashboard import build_dashboard_view
from ppa.pilot_session import PilotSession


def snap(*, usable=2, confirmed=0, unresolved=1, stale=0, actionable=1, questions=1):
    m0 = AuditMetric(0, ())
    def m(n, p): return AuditMetric(n, tuple(f"{p}{i}" for i in range(n)))
    return PilotAuditSnapshot(
        "ppa-pilot-audit/1", "t", True, 1, "/photos", None, None, 3,
        m(usable,"u"), m(usable,"u"), m(confirmed,"c"), m0, m(stale,"s"),
        m(unresolved,"x"), m0, m(actionable,"a"), m(questions,"q"),
        4 if questions else 0, {}, {}, {}, {}, 0)


def session(base, *, status="open", final=None):
    # dashboard does not validate hashes; PilotSession is already assumed loaded/validated.
    return PilotSession("ppa-pilot-session/1", "sid", "t", status, 1, "/photos", None, None,
                        base, "hash", (), "done" if status == "closed" else None,
                        final, "hash2" if final else None, None)


def test_dashboard_deltas_are_baseline_to_current():
    b = snap(usable=1, unresolved=2, actionable=2, questions=1)
    cur = snap(usable=3, confirmed=1, unresolved=0, actionable=0, questions=0)
    view = build_dashboard_view(session(b), cur)
    metrics = {m.label: m for m in view.metrics}
    assert metrics["Usable chronology"].delta == 2
    assert metrics["Unresolved"].delta == -2
    assert "No unresolved chronology" in view.suggested_action


def test_stale_decisions_take_guidance_priority():
    b = snap()
    cur = snap(stale=2, actionable=3)
    view = build_dashboard_view(session(b), cur)
    assert "stale decisions first" in view.suggested_action


def test_open_session_without_refresh_is_not_presented_as_current():
    view = build_dashboard_view(session(snap()), None)
    assert not view.current_available
    assert view.metrics == ()
    assert "Refresh current state" in view.suggested_action


def test_closed_session_uses_final_snapshot():
    b = snap(usable=1, unresolved=2)
    fin = snap(usable=2, unresolved=1)
    view = build_dashboard_view(session(b, status="closed", final=fin))
    assert view.current_available
    assert "Pilot closed" in view.suggested_action
