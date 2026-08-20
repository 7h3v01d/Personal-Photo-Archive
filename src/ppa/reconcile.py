"""Date Reliability Engine — Phase 6, Slice 3.0.1 (independent calendar evidence).

Slices 1–2 could add doubt but were forbidden from concluding a date is *wrong*
from suspicion or filename order alone. Slice 3 admits evidence independent of
the camera clock that addresses the calendar date itself, so escalation to
LIKELY_WRONG becomes earned — and the first TRUSTED dates appear.

Pure, storage-agnostic core. Read-only and deterministic: layers a FINAL
assessment over the Slice-2 combined rating; never mutates anything.

Central discipline (the fix behind 3.0.1): **the final rating is not evidence
about why that rating exists.** Slice 3 must decide from what INDEPENDENT
evidence it was given, never by reading a prior enum. So it records an explicit
``evidence_effect`` and only propagates a reset-run condemnation from a real
independent contradiction — never from a rating that arrived from Slice 2.

Only user anchors, GPS date (satellite-derived), and camera manufacture floors
are admitted. See docs/PHASE6_SLICE3_DESIGN.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from ppa.dating import Reliability

DEFAULT_DATE_TOLERANCE = timedelta(days=1)


class EvidenceEffect(str, Enum):
    NONE = "none"            # no independent Slice-3 evidence about this photo
    SUPPORT = "support"      # independent evidence agrees with the recorded date
    CONTRADICT = "contradict"  # independent evidence disagrees with the recorded date


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC; normalise aware ones to UTC. One
    convention everywhere, so comparisons never raise on naive/aware mixing."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class CalendarEvidence:
    """Independent-of-clock calendar evidence about one photo. Any field may be
    absent. Validated on construction so invalid states can't reach the engine
    (important before 3.1 persistence)."""
    manufacture_floor: datetime | None = None
    anchor_start: datetime | None = None
    anchor_end: datetime | None = None
    anchor_exact: bool = False
    gps_date: datetime | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.anchor_exact and self.anchor_start is None:
            raise ValueError("anchor_exact requires anchor_start")
        if self.anchor_end is not None and self.anchor_start is None:
            raise ValueError("anchor_end requires anchor_start")
        if self.anchor_end is not None and self.anchor_start is not None \
                and _as_utc(self.anchor_end) < _as_utc(self.anchor_start):
            raise ValueError("anchor_end must be >= anchor_start")
        if self.anchor_exact and self.anchor_end is not None:
            raise ValueError("anchor_exact is a single date; do not also set anchor_end")


@dataclass
class ReconcilablePhoto:
    file_id: str
    candidate: datetime | None
    reliability: Reliability
    reset_group: str | None = None
    evidence: CalendarEvidence = field(default_factory=CalendarEvidence)


@dataclass
class FinalAssessment:
    file_id: str
    reliability: Reliability
    date: datetime | None
    evidence_effect: EvidenceEffect = EvidenceEffect.NONE
    independent_contradiction: bool = False       # a real Slice-3 contradiction occurred
    reasons: list[str] = field(default_factory=list)
    evidence_conflicts: list[str] = field(default_factory=list)  # recorded, not hidden
    corrected_hint: datetime | None = None
    changed: bool = False


def _within(a: datetime, b: datetime, tol: timedelta) -> bool:
    return abs(_as_utc(a) - _as_utc(b)) <= tol


def _d(dt: datetime) -> str:
    return _as_utc(dt).date().isoformat()


def _anchor_contradicts(candidate: datetime, ev: CalendarEvidence, tol: timedelta) -> bool:
    if ev.anchor_start is None:
        return False
    if ev.anchor_end is None:                      # exact anchor
        return not _within(candidate, ev.anchor_start, tol)
    c, s, e = _as_utc(candidate), _as_utc(ev.anchor_start), _as_utc(ev.anchor_end)
    return c < s - tol or c > e + tol


def _evaluate_one(p: ReconcilablePhoto, tol: timedelta) -> FinalAssessment:
    ev = p.evidence
    cand = p.candidate
    fa = FinalAssessment(p.file_id, p.reliability, cand)

    # 1. Exact human anchor is authoritative -> TRUSTED at the anchored date.
    #    Conflicting independent evidence is RECORDED, not silently discarded.
    if ev.anchor_exact:
        fa.reliability = Reliability.TRUSTED
        fa.date = ev.anchor_start
        fa.evidence_effect = EvidenceEffect.SUPPORT
        if cand is not None and not _within(cand, ev.anchor_start, tol):
            fa.independent_contradiction = True    # anchor disproves the recorded date
            fa.reasons.append(f"User-confirmed date {_d(ev.anchor_start)} overrides the "
                              f"recorded {_d(cand)} (which was wrong).")
        else:
            fa.reasons.append(f"User-confirmed date {_d(ev.anchor_start)}.")
        if ev.gps_date is not None and not _within(ev.anchor_start, ev.gps_date, tol):
            fa.evidence_conflicts.append(
                f"GPS date {_d(ev.gps_date)} conflicts with the user anchor "
                f"{_d(ev.anchor_start)} (anchor kept as authoritative).")
        if ev.manufacture_floor is not None \
                and _as_utc(ev.anchor_start) < _as_utc(ev.manufacture_floor):
            fa.evidence_conflicts.append(
                f"User anchor {_d(ev.anchor_start)} precedes the camera manufacture "
                f"floor {_d(ev.manufacture_floor)} (anchor kept as authoritative).")
        fa.changed = fa.reliability != p.reliability or fa.date != cand
        return fa

    # 2. Independent contradictions of the recorded date -> LIKELY_WRONG.
    if cand is not None:
        contradictions: list[str] = []
        if ev.manufacture_floor is not None and _as_utc(cand) < _as_utc(ev.manufacture_floor):
            contradictions.append(
                f"Recorded date {_d(cand)} precedes the camera's earliest plausible "
                f"date {_d(ev.manufacture_floor)} (model did not exist yet).")
        if _anchor_contradicts(cand, ev, tol):
            span = (_d(ev.anchor_start) if ev.anchor_end is None
                    else f"{_d(ev.anchor_start)}…{_d(ev.anchor_end)}")
            contradictions.append(f"Recorded date {_d(cand)} is outside the "
                                  f"user-provided window {span}.")
            fa.corrected_hint = ev.anchor_start
        if ev.gps_date is not None and not _within(cand, ev.gps_date, tol):
            contradictions.append(f"Recorded date {_d(cand)} disagrees with the "
                                  f"independent GPS date {_d(ev.gps_date)}.")
            fa.corrected_hint = ev.gps_date

        if contradictions:
            fa.reliability = Reliability.LIKELY_WRONG
            fa.evidence_effect = EvidenceEffect.CONTRADICT
            fa.independent_contradiction = True
            fa.reasons = contradictions
            fa.changed = fa.reliability != p.reliability
            return fa

        # 3. GPS corroboration. Only PROBABLY_VALID may be raised to TRUSTED —
        #    Slice 3 doesn't know WHY a QUESTIONABLE/LIKELY_WRONG was doubted, so
        #    it must not erase that doubt just because a date repeats.
        if ev.gps_date is not None and _within(cand, ev.gps_date, tol):
            fa.evidence_effect = EvidenceEffect.SUPPORT
            if p.reliability is Reliability.PROBABLY_VALID:
                fa.reliability = Reliability.TRUSTED
                fa.reasons.append(f"Recorded date corroborated by the independent GPS "
                                  f"date {_d(ev.gps_date)}.")
            elif p.reliability is Reliability.LIKELY_WRONG:
                fa.evidence_conflicts.append(
                    f"GPS date {_d(ev.gps_date)} agrees with the recorded date, but an "
                    "earlier check already judged that date likely wrong; not trusting.")
            else:  # QUESTIONABLE and others: agreement noted, doubt not resolved
                fa.reasons.append(f"GPS date {_d(ev.gps_date)} agrees with the recorded "
                                  f"date, but the prior doubt is unresolved; unchanged.")
            fa.changed = fa.reliability != p.reliability
            return fa

    # 4. No independent evidence: the Slice-2 result stands unchanged.
    return fa


def reconcile(
    photos: list[ReconcilablePhoto],
    *,
    date_tolerance: timedelta = DEFAULT_DATE_TOLERANCE,
) -> dict[str, FinalAssessment]:
    """Reconcile Slice-2 ratings against independent calendar evidence. Pure and
    deterministic. A reset run is condemned as a whole only when a real Slice-3
    contradiction lands on one of its frames (never merely because a frame
    already carried a LIKELY_WRONG rating from an earlier layer)."""
    results = {p.file_id: _evaluate_one(p, date_tolerance) for p in photos}

    groups: dict[str, list[str]] = {}
    for p in photos:
        if p.reset_group is not None:
            groups.setdefault(p.reset_group, []).append(p.file_id)

    for ids in groups.values():
        # Trigger ONLY from an actual independent contradiction, not from a
        # rating that arrived from Slice 2.
        if not any(results[i].independent_contradiction for i in ids):
            continue
        for i in ids:
            fa = results[i]
            if fa.evidence_effect is not EvidenceEffect.NONE:
                continue  # a frame with its own independent evidence keeps its rating
            if fa.reliability in (Reliability.TRUSTED, Reliability.LIKELY_WRONG):
                continue
            fa.reliability = Reliability.LIKELY_WRONG
            fa.reasons.append(
                "Part of a clock-reset run disproved by independent calendar evidence "
                "on another frame; the shared reset date cannot be real.")
            fa.changed = True

    return results
