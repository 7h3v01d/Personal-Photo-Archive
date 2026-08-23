"""Phase 7.4 — read-only dashboard projections for pilot sessions.

This module deliberately contains no Qt and performs no database writes.  It turns
an integrity-checked :class:`PilotSession` plus an optional current audit snapshot
into a compact, deterministic view model for the desktop dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass

from ppa.pilot_audit import PilotAuditSnapshot, compare_pilot_audits
from ppa.pilot_session import PilotSession


@dataclass(frozen=True)
class DashboardMetric:
    label: str
    baseline: int
    current: int
    delta: int


@dataclass(frozen=True)
class PilotDashboardView:
    session_id: str
    status: str
    library_root: str
    scope_label: str
    checkpoint_count: int
    current_available: bool
    metrics: tuple[DashboardMetric, ...]
    suggested_action: str


def _scope_label(session: PilotSession) -> str:
    if session.explicit_file_ids is not None:
        return f"explicit selection ({len(session.explicit_file_ids)} files)"
    if session.directory_prefix:
        return session.directory_prefix
    return "entire library"


def build_dashboard_view(session: PilotSession,
                         current: PilotAuditSnapshot | None = None) -> PilotDashboardView:
    """Build a deterministic presentation model without changing archive/session state."""
    baseline = session.baseline
    snap = current
    if snap is None and session.status == "closed":
        snap = session.final

    metrics: tuple[DashboardMetric, ...] = ()
    if snap is not None:
        comparison = compare_pilot_audits(baseline, snap)
        metrics = (
            DashboardMetric("Usable chronology", comparison.usable_chronology.before,
                            comparison.usable_chronology.after, comparison.usable_chronology.delta),
            DashboardMetric("Confirmed current", comparison.confirmed_current.before,
                            comparison.confirmed_current.after, comparison.confirmed_current.delta),
            DashboardMetric("Unresolved", comparison.unresolved.before,
                            comparison.unresolved.after, comparison.unresolved.delta),
            DashboardMetric("Stale decisions", comparison.stale_decisions.before,
                            comparison.stale_decisions.after, comparison.stale_decisions.delta),
            DashboardMetric("Actionable review", comparison.actionable_review.before,
                            comparison.actionable_review.after, comparison.actionable_review.delta),
            DashboardMetric("Anchor questions", comparison.anchor_questions.before,
                            comparison.anchor_questions.after, comparison.anchor_questions.delta),
        )

    if session.status == "closed":
        suggested = "Pilot closed — retain the session artifact as the audit record."
    elif snap is None:
        suggested = "Refresh current state, then continue Date Review or Unresolved Memories."
    elif snap.stale_decisions.count:
        suggested = "Review stale decisions first; their underlying bytes or evidence changed."
    elif snap.actionable_review.count:
        suggested = "Continue Date Review; actionable chronology work remains."
    elif snap.unresolved.count:
        suggested = "Browse Unresolved Memories; no higher-priority review work remains."
    else:
        suggested = "No unresolved chronology remains in this pilot scope; consider closing the pilot."

    return PilotDashboardView(
        session.session_id, session.status, session.library_root, _scope_label(session),
        len(session.checkpoints), snap is not None, metrics, suggested,
    )
