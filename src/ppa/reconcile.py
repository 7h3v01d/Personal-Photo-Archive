"""Date Reliability Engine — Phase 6, Slice 3.2.1 (independent calendar evidence).

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
    doubts: list = field(default_factory=list)   # structured doubt codes (Slice 1+2)
    reset_group_strong: bool = False   # the reset group is a credible SINGLE device


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
            else:  # QUESTIONABLE: resolve only if EVERY doubt is one an independent
                #                   date can settle; unrelated doubts keep it doubtful.
                resolvable = bool(p.doubts) and all(
                    getattr(d, "resolvable_by_independent_date", False) for d in p.doubts)
                if resolvable:
                    fa.reliability = Reliability.TRUSTED
                    codes = ", ".join(getattr(d, "value", str(d)) for d in p.doubts)
                    fa.reasons.append(f"Independent GPS date {_d(ev.gps_date)} confirms the "
                                      f"recorded date, resolving the only doubt(s) ({codes}).")
                else:
                    unresolved = [getattr(d, "value", str(d)) for d in p.doubts
                                  if not getattr(d, "resolvable_by_independent_date", False)]
                    fa.reasons.append(
                        f"GPS date {_d(ev.gps_date)} agrees with the recorded date, but "
                        f"unrelated doubt remains ({', '.join(unresolved) or 'unspecified'}); "
                        "stays QUESTIONABLE.")
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
    strong: dict[str, bool] = {}
    for p in photos:
        if p.reset_group is not None:
            groups.setdefault(p.reset_group, []).append(p.file_id)
            strong[p.reset_group] = strong.get(p.reset_group, True) and p.reset_group_strong

    for gid, ids in groups.items():
        # Group-level independent-evidence state. A member "contradicts" the
        # shared reset date if its own independent evidence disagrees with its
        # candidate; it "supports" the shared date if independent evidence agrees.
        contradicts = [i for i in ids if results[i].independent_contradiction]
        supports = [i for i in ids
                    if results[i].evidence_effect is EvidenceEffect.SUPPORT
                    and not results[i].independent_contradiction]
        if not contradicts:
            continue  # nothing independently disproves the shared date

        # Conflicting group evidence: some frames confirm, some disprove the same
        # shared date. Fail conservative — don't condemn frames lacking their own
        # evidence; each frame with evidence keeps its individual result.
        if supports and contradicts:
            for i in ids:
                fa = results[i]
                if fa.evidence_effect is EvidenceEffect.NONE:
                    fa.reasons.append(
                        "Independent evidence within this reset group conflicts (some "
                        "frames confirm, some disprove the shared date); group "
                        "propagation withheld.")
            continue

        # Contradiction only. Propagate a whole-run condemnation ONLY if the group
        # is a credible single physical device — otherwise the "shared clock"
        # premise isn't established (a serial-less model group may be two bodies),
        # so the contradiction applies only to its own frame.
        if not strong.get(gid, False):
            for i in ids:
                fa = results[i]
                if fa.evidence_effect is EvidenceEffect.NONE:
                    fa.reasons.append(
                        "A frame in this reset-looking group was disproved by "
                        "independent evidence, but the group is not a confirmed single "
                        "device (no unique camera serial); condemnation is not propagated.")
            continue

        for i in ids:
            fa = results[i]
            if fa.evidence_effect is not EvidenceEffect.NONE:
                continue
            if fa.reliability in (Reliability.TRUSTED, Reliability.LIKELY_WRONG):
                continue
            fa.reliability = Reliability.LIKELY_WRONG
            fa.reasons.append(
                "Part of a clock-reset run (confirmed single device) disproved by "
                "independent calendar evidence on another frame; the shared reset "
                "date cannot be real.")
            fa.changed = True

    return results


# --- catalogue integration (Slice 3.1) ---------------------------------------

def _parse_gps_datestamp(value: str) -> datetime | None:
    """Parse an EXIF GPSDateStamp ('YYYY:MM:DD'), satellite-derived, to UTC."""
    try:
        return datetime.strptime(value.strip(), "%Y:%m:%d").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def analyse_library_reconciled(
    conn,
    *,
    camera_floors=None,
    date_tolerance: timedelta = DEFAULT_DATE_TOLERANCE,
    **assess_kwargs,
):
    """Full Slice 1→2→3 read-only assessment over the catalogue.

    Runs the cross-photo chronology, then reconciles each photo against
    independent calendar evidence: GPS date (exif-gps:GPSDateStamp), user anchors
    (anchors table), and camera manufacture floors (optional config). Returns
    ``(findings, results)`` where results maps file_id -> FinalAssessment.
    """
    from ppa import anchors as anchors_mod
    from ppa.chronology import _is_strong_serial
    from ppa.chronology import analyse_library as _chron

    findings, chron = _chron(conn, **assess_kwargs)

    # Credible unique-device identity per file (a placeholder/absent serial is not).
    strong_by_file = {
        r["id"]: _is_strong_serial(r["serial"])
        for r in conn.execute(
            "SELECT f.id, c.serial FROM files f LEFT JOIN cameras c ON c.id = f.camera_id "
            "WHERE f.presence_status = 'present'")
    }

    # Which reset-pattern run (if any) each file belongs to, and whether that run
    # is a confirmed single device (every member has a credible serial).
    reset_group: dict[str, str] = {}
    reset_group_strong: dict[str, bool] = {}
    for n, f in enumerate(x for x in findings if x.kind == "reset_pattern"):
        gid = f"reset-{n}"
        gstrong = all(strong_by_file.get(fid, False) for fid in f.file_ids)
        for fid in f.file_ids:
            reset_group[fid] = gid
            reset_group_strong[fid] = gstrong

    anchor_list = anchors_mod.list_anchors(conn)

    rows = conn.execute(
        "SELECT f.id, f.relative_path, f.filename, f.library_id, "
        "c.make AS cam_make, c.model AS cam_model "
        "FROM files f LEFT JOIN cameras c ON c.id = f.camera_id "
        "WHERE f.presence_status = 'present'"
    ).fetchall()

    photos: list[ReconcilablePhoto] = []
    for r in rows:
        fid = r["id"]
        pc = chron.get(fid)
        if pc is None:
            continue
        rel = (r["relative_path"] or r["filename"]).replace("\\", "/")
        directory = rel.rsplit("/", 1)[0] if "/" in rel else ""

        # GPS date (independent of the camera clock), current revision.
        gps_row = conn.execute(
            "SELECT value FROM metadata_observations WHERE file_id = ? "
            "AND source = 'exif-gps' AND key = 'GPSDateStamp' AND file_revision_id = "
            "(SELECT current_revision_id FROM files WHERE id = ?)",
            (fid, fid),
        ).fetchone()
        gps_date = _parse_gps_datestamp(gps_row["value"]) if gps_row else None

        floor = (camera_floors.floor_for(r["cam_make"], r["cam_model"])
                 if camera_floors is not None else None)

        anchor = anchors_mod.resolve_for(anchor_list, file_id=fid,
                                         directory=directory, library_id=r["library_id"])
        if anchor is not None:
            start = datetime.combine(anchor.start_date, datetime.min.time(),
                                     tzinfo=timezone.utc)
            end = (datetime.combine(anchor.end_date, datetime.min.time(),
                                    tzinfo=timezone.utc) if anchor.end_date else None)
            ev = CalendarEvidence(manufacture_floor=floor, anchor_start=start,
                                  anchor_end=end, anchor_exact=(anchor.kind == "exact"),
                                  gps_date=gps_date)
        else:
            ev = CalendarEvidence(manufacture_floor=floor, gps_date=gps_date)

        photos.append(ReconcilablePhoto(fid, pc.candidate, pc.reliability,
                                        reset_group.get(fid), ev, list(pc.doubts),
                                        reset_group_strong.get(fid, False)))

    return findings, reconcile(photos, date_tolerance=date_tolerance)


def export_reconciliation_csv(conn, path, *, camera_floors=None) -> int:
    """Write the full Slice 1→3 assessment to a CSV for reviewing against a real
    collection (read-only). Rows sorted by rating so the doubtful ones surface.
    Returns the number of photos written."""
    import csv as _csv

    _, results = analyse_library_reconciled(conn, camera_floors=camera_floors)
    paths = {r["id"]: (r["relative_path"] or r["filename"])
             for r in conn.execute(
                 "SELECT id, relative_path, filename FROM files "
                 "WHERE presence_status = 'present'")}
    order = {"LIKELY_WRONG": 0, "QUESTIONABLE": 1, "UNKNOWN": 2,
             "PROBABLY_VALID": 3, "TRUSTED": 4}
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["file_id", "path", "rating", "candidate_or_anchored_date",
                    "changed_by_evidence", "reasons", "evidence_conflicts"])
        for fid, fa in sorted(results.items(),
                              key=lambda kv: (order.get(kv[1].reliability.value, 9),
                                              paths.get(kv[0], ""))):
            w.writerow([fid, paths.get(fid, ""), fa.reliability.value,
                        fa.date.date().isoformat() if fa.date else "",
                        "yes" if fa.changed else "",
                        " | ".join(fa.reasons), " | ".join(fa.evidence_conflicts)])
    return len(results)
