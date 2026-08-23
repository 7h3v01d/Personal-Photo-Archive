"""Phase 8.0 — provenance-aware chronology timeline foundation.

Read-only projection of the accepted Phase-6/7 chronology state.  This module
never infers a new date and never mutates observations, reconstructions, anchors,
or source files.  Its only job is to decide which already-supported date claim a
photo is allowed to occupy on a timeline.

Precedence:
  1. fresh human-confirmed reconstruction
  2. TRUSTED / PROBABLY_VALID reconciled date
  3. fresh proposed reconstruction (tentative lane only)
  4. everything else is unplaced

Ranges remain ranges.  Stale decisions are never treated as current chronology.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from sqlite3 import Connection
from typing import Callable, Collection

from ppa.dating import Reliability
from ppa.pilot import PilotAnalysisCancelled, PilotScope, _scope_ids
from ppa.reconcile import analyse_library_reconciled
from ppa.reconstruct_catalogue import list_reconstructions

TIMELINE_SCHEMA = "ppa-timeline/1"


@dataclass(frozen=True)
class TimelineItem:
    file_id: str
    filename: str
    lane: str                 # placed | range | tentative | unplaced
    source: str               # confirmed_reconstruction | reconciled | proposed_reconstruction | none
    start_date: str | None
    end_date: str | None
    reliability: str
    confidence: str | None
    method: str | None
    reconstruction_status: str | None
    content_stale: bool
    evidence_stale: bool
    reason: str

    @property
    def stale(self) -> bool:
        return self.content_stale or self.evidence_stale


@dataclass(frozen=True)
class TimelineBucket:
    key: str
    count: int
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class TimelineView:
    schema: str
    generated_at: str
    read_only: bool
    scope: PilotScope
    items: tuple[TimelineItem, ...]
    lanes: dict[str, TimelineBucket]
    years: tuple[TimelineBucket, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


def _checkpoint(progress_cb: Callable[[str], None] | None,
                cancel_cb: Callable[[], bool] | None,
                message: str) -> None:
    if cancel_cb is not None and cancel_cb():
        raise PilotAnalysisCancelled()
    if progress_cb is not None:
        progress_cb(message)


def _iso_day(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()


def _bucket(key: str, ids) -> TimelineBucket:
    vals = tuple(sorted(set(ids)))
    return TimelineBucket(key, len(vals), vals)


def build_timeline(conn: Connection, *, library_id: int,
                   directory_prefix: str | None = None,
                   file_ids: Collection[str] | None = None,
                   camera_floors=None,
                   generated_at: str | None = None,
                   progress_cb: Callable[[str], None] | None = None,
                   cancel_cb: Callable[[], bool] | None = None) -> TimelineView:
    """Build a deterministic, read-only timeline projection for one scope.

    The expensive chronology work is intentionally suitable for a worker thread;
    callers may supply progress/cancellation hooks.  No writes are performed.
    """
    _checkpoint(progress_cb, cancel_cb, "Timeline: selecting library scope…")
    selected, scope = _scope_ids(conn, library_id, directory_prefix, file_ids)

    _checkpoint(progress_cb, cancel_cb, "Timeline: reconciling usable chronology…")
    _findings, final = analyse_library_reconciled(conn, camera_floors=camera_floors)

    _checkpoint(progress_cb, cancel_cb, "Timeline: checking reconstruction freshness…")
    stored = {r.file_id: r for r in list_reconstructions(conn, camera_floors=camera_floors)}

    rows = conn.execute(
        "SELECT id, filename FROM files WHERE library_id=? AND presence_status='present'",
        (library_id,),
    ).fetchall()
    names = {r["id"]: r["filename"] for r in rows if r["id"] in selected}

    out: list[TimelineItem] = []
    for fid in sorted(selected):
        if cancel_cb is not None and cancel_cb():
            raise PilotAnalysisCancelled()
        fa = final.get(fid)
        reliability = fa.reliability.value if fa else Reliability.UNKNOWN.value
        rec = stored.get(fid)

        # 1. Fresh, human-confirmed reconstruction is authoritative.
        if rec is not None and rec.status == "confirmed" and not rec.stale:
            lane = "range" if rec.end_date is not None else "placed"
            out.append(TimelineItem(
                fid, names.get(fid, fid), lane, "confirmed_reconstruction",
                rec.start_date.isoformat(), rec.end_date.isoformat() if rec.end_date else None,
                reliability, rec.confidence, rec.method, rec.status,
                False, False,
                "Fresh human-confirmed reconstruction." if lane == "placed" else
                "Fresh human-confirmed date range; precision is intentionally preserved.",
            ))
            continue

        # 2. A reconciled date with strong enough current reliability can stand on
        #    its own.  This includes clean recorded chronology and independent
        #    evidence that promoted the date to TRUSTED.
        if fa is not None and fa.date is not None and fa.reliability in {
                Reliability.TRUSTED, Reliability.PROBABLY_VALID}:
            content_stale = bool(rec.content_stale) if rec is not None else False
            evidence_stale = bool(rec.evidence_stale) if rec is not None else False
            out.append(TimelineItem(
                fid, names.get(fid, fid), "placed", "reconciled",
                _iso_day(fa.date), None, reliability,
                rec.confidence if rec is not None else None,
                rec.method if rec is not None else None,
                rec.status if rec is not None else None,
                content_stale, evidence_stale,
                "Current reconciled chronology is trusted/probably valid; no reconstruction authority is required.",
            ))
            continue

        # 3. A fresh proposal may be useful visually, but remains a tentative lane
        #    until a human confirms it.  It never masquerades as authoritative.
        if rec is not None and rec.status == "proposed" and not rec.stale:
            out.append(TimelineItem(
                fid, names.get(fid, fid), "tentative", "proposed_reconstruction",
                rec.start_date.isoformat(), rec.end_date.isoformat() if rec.end_date else None,
                reliability, rec.confidence, rec.method, rec.status,
                False, False,
                "Fresh reconstruction proposal; shown tentatively until human review.",
            ))
            continue

        # 4. Anything else is explicitly unplaced.  Preserve why rather than
        #    forcing the file onto a date axis.
        cs = bool(rec.content_stale) if rec is not None else False
        es = bool(rec.evidence_stale) if rec is not None else False
        if rec is not None and rec.stale:
            reason = "Stored reconstruction is stale and cannot place this photo until reviewed."
        elif rec is not None and rec.status == "rejected":
            reason = "Reconstruction was rejected and current chronology is not reliable enough to place."
        elif reliability == Reliability.LIKELY_WRONG.value:
            reason = "Recorded chronology is likely wrong and no fresh accepted replacement exists."
        elif reliability == Reliability.QUESTIONABLE.value:
            reason = "Recorded chronology is questionable and no fresh accepted replacement exists."
        else:
            reason = "No defensible current date is available."
        out.append(TimelineItem(
            fid, names.get(fid, fid), "unplaced", "none", None, None,
            reliability, rec.confidence if rec else None, rec.method if rec else None,
            rec.status if rec else None, cs, es, reason,
        ))

    # Chronological lanes first, deterministic tie-breaking by filename/id.
    lane_rank = {"placed": 0, "range": 1, "tentative": 2, "unplaced": 3}
    out.sort(key=lambda i: (
        lane_rank[i.lane], i.start_date or "9999-12-31", i.end_date or i.start_date or "9999-12-31",
        i.filename.casefold(), i.file_id,
    ))

    lane_ids: dict[str, list[str]] = {k: [] for k in ("placed", "range", "tentative", "unplaced")}
    year_ids: dict[str, list[str]] = {}
    for item in out:
        lane_ids[item.lane].append(item.file_id)
        if item.start_date:
            year_ids.setdefault(item.start_date[:4], []).append(item.file_id)

    lanes = {k: _bucket(k, lane_ids[k]) for k in ("placed", "range", "tentative", "unplaced")}
    years = tuple(_bucket(y, ids) for y, ids in sorted(year_ids.items()))

    _checkpoint(progress_cb, cancel_cb, "Timeline: ready.")
    return TimelineView(
        TIMELINE_SCHEMA,
        generated_at or datetime.now().astimezone().isoformat(),
        True,
        scope,
        tuple(out),
        lanes,
        years,
    )


def concise_text(view: TimelineView) -> str:
    lines = [
        "PPA Timeline — provenance-aware chronology",
        "==========================================",
        f"Files in scope: {len(view.items)}",
        "",
        "Lanes",
        "-----",
        f"Placed:     {view.lanes['placed'].count}",
        f"Ranges:     {view.lanes['range'].count}",
        f"Tentative:  {view.lanes['tentative'].count}",
        f"Unplaced:   {view.lanes['unplaced'].count}",
    ]
    if view.years:
        lines += ["", "Years", "-----"]
        lines.extend(f"{b.key}: {b.count}" for b in view.years)
    lines += ["", "Read-only: no dates, evidence, decisions, or source photos were modified."]
    return "\n".join(lines)
