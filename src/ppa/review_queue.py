"""Phase 7.2.2 — deterministic, read-only date review queue.

The queue is a workflow/read-model layered on Phase 7.2.1 pilot analysis and the
accepted reconstruction persistence model.  It never creates chronology facts or
changes evidence/decisions.  It answers only: *what should a human look at next,
and why?*
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from sqlite3 import Connection
from typing import Callable, Collection

from ppa.pilot import PilotReport, analyse_pilot
from ppa.anchor_opportunities import build_anchor_questions
from ppa.reconstruct_catalogue import list_reconstructions

QUEUE_SCHEMA = "ppa-date-review-queue/1"
_PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}
_ACTION_ORDER = {
    "REFRESH_STALE_PROPOSAL": 0,
    "REOPEN_REFRESH_DECISION": 1,
    "REVIEW_CURRENT_PROPOSAL": 2,
    "RESOLVE_CONFLICT": 3,
    "HIGH_LEVERAGE_ANCHOR": 4,
    "INVESTIGATE_UNCERTAIN": 5,
    "INSUFFICIENT_EVIDENCE": 6,
}


@dataclass(frozen=True)
class ReviewQueueItem:
    file_id: str
    filename: str
    priority: str
    action: str
    reason: str
    reliability: str
    reconstruction_status: str | None
    reconstruction_confidence: str | None
    stale: bool
    affected_file_ids: tuple[str, ...]
    affected_count: int


@dataclass(frozen=True)
class ReviewQueue:
    schema: str
    library_id: int
    total_items: int
    actionable_items: int
    items: tuple[ReviewQueueItem, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), indent=2 if pretty else None,
                          sort_keys=True, separators=None if pretty else (",", ":"))

    def actionable(self) -> tuple[ReviewQueueItem, ...]:
        return tuple(i for i in self.items if i.priority != "D")


def _membership(report: PilotReport, mapping: dict[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, bucket in mapping.items():
        for fid in bucket.file_ids:
            out[fid] = key
    return out


def build_review_queue(conn: Connection, *, library_id: int,
                       directory_prefix: str | None = None,
                       file_ids: Collection[str] | None = None,
                       camera_floors=None,
                       progress_cb: Callable[[str], None] | None = None,
                       cancel_cb: Callable[[], bool] | None = None,
                       report: PilotReport | None = None) -> ReviewQueue:
    """Return a deterministic queue ordered by human-review value.

    This function is read-only.  All chronology/reconstruction facts come from
    ``analyse_pilot`` / the accepted persistence read model; this layer merely
    assigns an action and an explanation to each file in the selected scope.
    """
    if report is None:
        report = analyse_pilot(conn, library_id=library_id,
                               directory_prefix=directory_prefix, file_ids=file_ids,
                               camera_floors=camera_floors, generated_at="queue",
                               progress_cb=progress_cb, cancel_cb=cancel_cb)
    elif report.scope.library_id != library_id:
        raise ValueError("supplied pilot report belongs to a different library")
    rel_by = _membership(report, report.reliability)
    priority_by = _membership(report, report.review_priority)
    conflict_kinds: dict[str, list[str]] = {}
    for conflict in report.conflicts:
        for fid in conflict.file_ids:
            conflict_kinds.setdefault(fid, []).append(conflict.kind)
    questions = build_anchor_questions(conn, library_id=library_id, report=report, camera_floors=camera_floors)
    opportunities = {q.file_id: q for q in questions.questions}
    recs = {r.file_id: r for r in list_reconstructions(conn, camera_floors=camera_floors)}

    # Scope is authoritative: names are fetched only for ids already selected by
    # the pilot report so queue construction cannot leak a neighbouring library.
    scoped_ids = set().union(*(b.file_ids for b in report.review_priority.values()))
    if scoped_ids:
        marks = ",".join("?" for _ in scoped_ids)
        rows = conn.execute(
            f"SELECT id, filename FROM files WHERE library_id=? AND id IN ({marks})",
            (library_id, *sorted(scoped_ids))).fetchall()
        names = {r["id"]: r["filename"] for r in rows}
    else:
        names = {}

    items: list[ReviewQueueItem] = []
    for fid in sorted(scoped_ids):
        priority = priority_by[fid]
        reliability = rel_by.get(fid, "UNKNOWN")
        rec = recs.get(fid)
        opp = opportunities.get(fid)
        conflicts = sorted(conflict_kinds.get(fid, []))

        affected: tuple[str, ...] = ()
        if rec is not None and rec.stale and rec.status == "proposed":
            action = "REFRESH_STALE_PROPOSAL"
            reason = "Stored proposal is stale; refresh it against current bytes/evidence before review."
        elif rec is not None and rec.stale and rec.status in ("confirmed", "rejected"):
            action = "REOPEN_REFRESH_DECISION"
            reason = (f"Previous {rec.status} decision is stale; preserve it historically, "
                      "then reopen and refresh before making a new decision.")
        elif rec is not None and rec.status == "proposed":
            action = "REVIEW_CURRENT_PROPOSAL"
            reason = f"Current {rec.confidence} reconstruction is ready for human review."
        elif conflicts:
            action = "RESOLVE_CONFLICT"
            reason = "Chronology/evidence conflict requires judgement: " + ", ".join(conflicts) + "."
        elif opp is not None:
            action = "HIGH_LEVERAGE_ANCHOR"
            affected = tuple(sorted(opp.affected_file_ids))
            reason = opp.reason
        elif reliability in ("QUESTIONABLE", "LIKELY_WRONG"):
            action = "INVESTIGATE_UNCERTAIN"
            reason = f"Recorded chronology is {reliability.lower().replace('_', ' ')} with no current proposal."
        else:
            action = "INSUFFICIENT_EVIDENCE"
            reason = "No high-value date-review action is currently supported by the available evidence."

        items.append(ReviewQueueItem(
            fid, names.get(fid, fid), priority, action, reason, reliability,
            rec.status if rec else None, rec.confidence if rec else None,
            bool(rec and rec.stale), affected, len(affected)))

    if cancel_cb is not None and cancel_cb():
        from ppa.pilot import PilotAnalysisCancelled
        raise PilotAnalysisCancelled()
    if progress_cb is not None:
        progress_cb("Date Review: prioritising review actions…")
    items.sort(key=lambda i: (
        _PRIORITY_ORDER[i.priority], _ACTION_ORDER[i.action],
        -i.affected_count, i.filename.casefold(), i.file_id))
    return ReviewQueue(QUEUE_SCHEMA, library_id, len(items),
                       sum(1 for i in items if i.priority != "D"), tuple(items))


def concise_text(queue: ReviewQueue, *, include_d: bool = False) -> str:
    shown = queue.items if include_d else queue.actionable()
    lines = ["PPA Date Review Queue", "=====================", "",
             f"Library: {queue.library_id}",
             f"Actionable: {queue.actionable_items} / {queue.total_items}", ""]
    if not shown:
        lines.append("No review items in this scope.")
        return "\n".join(lines)
    for n, item in enumerate(shown, 1):
        leverage = f"; may affect {item.affected_count}" if item.affected_count else ""
        lines.append(f"{n:3}. [{item.priority}] {item.filename} — {item.action}{leverage}")
        lines.append(f"     {item.reason}")
    return "\n".join(lines)
