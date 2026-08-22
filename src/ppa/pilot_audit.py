"""Phase 7.2.7 — read-only pilot audit snapshots and comparisons.

The audit layer measures the state produced by the accepted Phase-6/7 read
models.  It never reconstructs new chronology and never mutates catalogue or
source files.  A truthful before/after comparison requires two explicit
snapshots; this module never invents a historical baseline from current data.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Callable, Collection

from ppa.anchor_opportunities import build_anchor_questions
from ppa.pilot import analyse_pilot
from ppa.review_queue import build_review_queue
from ppa.unresolved import build_unresolved_memories

AUDIT_SCHEMA = "ppa-pilot-audit/1"
COMPARISON_SCHEMA = "ppa-pilot-audit-comparison/1"


@dataclass(frozen=True)
class AuditMetric:
    count: int
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class PilotAuditSnapshot:
    schema: str
    generated_at: str
    read_only: bool
    library_id: int
    library_root: str
    directory_prefix: str | None
    explicit_file_ids: tuple[str, ...] | None
    total_files: int
    usable_chronology: AuditMetric
    usable_recorded_chronology: AuditMetric
    confirmed_current: AuditMetric
    proposed_current: AuditMetric
    stale_decisions: AuditMetric
    unresolved: AuditMetric
    conflicts: AuditMetric
    actionable_review: AuditMetric
    anchor_questions: AuditMetric
    max_anchor_leverage: int
    unresolved_categories: dict[str, int]
    reconstruction_counts: dict[str, int]
    reliability_counts: dict[str, int]
    integrity_counts: dict[str, int]
    audit_source_writes: int

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), indent=2 if pretty else None,
                          sort_keys=True, separators=None if pretty else (",", ":"))


@dataclass(frozen=True)
class AuditDelta:
    before: int
    after: int
    delta: int


@dataclass(frozen=True)
class PilotAuditComparison:
    schema: str
    same_scope: bool
    before_generated_at: str
    after_generated_at: str
    total_files: AuditDelta
    usable_chronology: AuditDelta
    confirmed_current: AuditDelta
    proposed_current: AuditDelta
    stale_decisions: AuditDelta
    unresolved: AuditDelta
    conflicts: AuditDelta
    actionable_review: AuditDelta
    anchor_questions: AuditDelta
    max_anchor_leverage: AuditDelta

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), indent=2 if pretty else None,
                          sort_keys=True, separators=None if pretty else (",", ":"))


def _metric(ids) -> AuditMetric:
    values = tuple(sorted(set(ids)))
    return AuditMetric(len(values), values)


def _snapshot_time(generated_at: str | None) -> str:
    return generated_at or datetime.now(timezone.utc).isoformat()


def build_pilot_audit(conn: Connection, *, library_id: int,
                      directory_prefix: str | None = None,
                      file_ids: Collection[str] | None = None,
                      camera_floors=None,
                      generated_at: str | None = None,
                      progress_cb: Callable[[str], None] | None = None,
                      cancel_cb: Callable[[], bool] | None = None) -> PilotAuditSnapshot:
    """Capture one truthful, read-only Phase-7 pilot audit snapshot."""
    if progress_cb:
        progress_cb("Pilot Audit: analysing chronology and reconstruction state…")
    report = analyse_pilot(conn, library_id=library_id,
                           directory_prefix=directory_prefix, file_ids=file_ids,
                           camera_floors=camera_floors, generated_at="audit",
                           progress_cb=progress_cb, cancel_cb=cancel_cb)
    if progress_cb:
        progress_cb("Pilot Audit: classifying unresolved memories…")
    unresolved = build_unresolved_memories(conn, library_id=library_id,
                                           directory_prefix=directory_prefix,
                                           file_ids=file_ids,
                                           camera_floors=camera_floors,
                                           progress_cb=progress_cb,
                                           cancel_cb=cancel_cb, report=report)
    if progress_cb:
        progress_cb("Pilot Audit: measuring review workload…")
    queue = build_review_queue(conn, library_id=library_id,
                               directory_prefix=directory_prefix, file_ids=file_ids,
                               camera_floors=camera_floors,
                               progress_cb=progress_cb, cancel_cb=cancel_cb,
                               report=report)
    questions = build_anchor_questions(conn, library_id=library_id,
                                       report=report, camera_floors=camera_floors)

    trusted = set(report.reliability.get("TRUSTED", _metric(())).file_ids)
    probable = set(report.reliability.get("PROBABLY_VALID", _metric(())).file_ids)
    recorded_usable = trusted | probable
    confirmed = set(report.reconstruction.get("confirmed_current", _metric(())).file_ids)
    usable = recorded_usable | confirmed
    proposed_current = set()
    stale = set()
    for key, bucket in report.reconstruction.items():
        if key == "proposed_current":
            proposed_current.update(bucket.file_ids)
        if key.endswith("_stale"):
            stale.update(bucket.file_ids)
    conflict_ids = {fid for conflict in report.conflicts for fid in conflict.file_ids
                    if conflict.kind != "STALE_HUMAN_DECISION"}
    actionable = {item.file_id for item in queue.actionable()}
    question_ids = {q.file_id for q in questions.questions}

    return PilotAuditSnapshot(
        AUDIT_SCHEMA, _snapshot_time(generated_at), True,
        report.scope.library_id, report.scope.library_root,
        report.scope.directory_prefix, report.scope.explicit_file_ids,
        report.total_files,
        _metric(usable), _metric(recorded_usable), _metric(confirmed),
        _metric(proposed_current), _metric(stale),
        _metric(i.file_id for i in unresolved.items), _metric(conflict_ids),
        _metric(actionable), _metric(question_ids),
        max((q.affected_count for q in questions.questions), default=0),
        {c.category: c.count for c in unresolved.categories},
        {k: v.count for k, v in report.reconstruction.items()},
        {k: v.count for k, v in report.reliability.items()},
        {k: v.count for k, v in report.integrity.items()},
        0,
    )


def _scope_key(s: PilotAuditSnapshot) -> tuple:
    return (s.library_root, s.directory_prefix, s.explicit_file_ids)


def _d(before: int, after: int) -> AuditDelta:
    return AuditDelta(before, after, after - before)


def compare_pilot_audits(before: PilotAuditSnapshot,
                         after: PilotAuditSnapshot) -> PilotAuditComparison:
    """Compare two explicit snapshots. Refuse cross-scope comparisons."""
    if before.schema != AUDIT_SCHEMA or after.schema != AUDIT_SCHEMA:
        raise ValueError("unsupported pilot audit schema")
    if _scope_key(before) != _scope_key(after):
        raise ValueError("pilot audit snapshots have different scopes")
    return PilotAuditComparison(
        COMPARISON_SCHEMA, True, before.generated_at, after.generated_at,
        _d(before.total_files, after.total_files),
        _d(before.usable_chronology.count, after.usable_chronology.count),
        _d(before.confirmed_current.count, after.confirmed_current.count),
        _d(before.proposed_current.count, after.proposed_current.count),
        _d(before.stale_decisions.count, after.stale_decisions.count),
        _d(before.unresolved.count, after.unresolved.count),
        _d(before.conflicts.count, after.conflicts.count),
        _d(before.actionable_review.count, after.actionable_review.count),
        _d(before.anchor_questions.count, after.anchor_questions.count),
        _d(before.max_anchor_leverage, after.max_anchor_leverage),
    )


def snapshot_from_dict(data: dict) -> PilotAuditSnapshot:
    if data.get("schema") != AUDIT_SCHEMA:
        raise ValueError("unsupported pilot audit schema")
    def m(name: str) -> AuditMetric:
        value = data[name]
        return AuditMetric(int(value["count"]), tuple(value["file_ids"]))
    return PilotAuditSnapshot(
        data["schema"], data["generated_at"], bool(data["read_only"]),
        int(data["library_id"]), data["library_root"], data.get("directory_prefix"),
        tuple(data["explicit_file_ids"]) if data.get("explicit_file_ids") is not None else None,
        int(data["total_files"]), m("usable_chronology"), m("usable_recorded_chronology"),
        m("confirmed_current"), m("proposed_current"), m("stale_decisions"),
        m("unresolved"), m("conflicts"), m("actionable_review"), m("anchor_questions"),
        int(data["max_anchor_leverage"]), dict(data["unresolved_categories"]),
        dict(data["reconstruction_counts"]), dict(data["reliability_counts"]),
        dict(data["integrity_counts"]), int(data["audit_source_writes"]),
    )


def concise_text(s: PilotAuditSnapshot) -> str:
    lines = ["PPA Phase 7 Pilot Audit", "=======================", "",
             f"Library: {s.library_id}", f"Files in scope: {s.total_files}", "",
             "Chronology outcome", "------------------",
             f"Usable chronology:      {s.usable_chronology.count}",
             f"  recorded usable:      {s.usable_recorded_chronology.count}",
             f"  confirmed current:    {s.confirmed_current.count}",
             f"Current proposals:      {s.proposed_current.count}",
             f"Stale decisions:        {s.stale_decisions.count}",
             f"Unresolved:             {s.unresolved.count}",
             f"Conflicts:              {s.conflicts.count}", "",
             "Human-work leverage", "-------------------",
             f"Actionable review items:{s.actionable_review.count:7}",
             f"Anchor questions:       {s.anchor_questions.count:7}",
             f"Best anchor leverage:   {s.max_anchor_leverage:7} other photo(s)", "",
             "Integrity", "---------",
             f"Current integrity flags:{sum(s.integrity_counts.values()):7}",
             f"Source writes by audit: {s.audit_source_writes:7}", "",
             "Note: this is a snapshot of current state. A truthful before/after result",
             "requires comparison with an earlier explicit audit snapshot."]
    return "\n".join(lines)


def comparison_text(c: PilotAuditComparison) -> str:
    def row(label: str, d: AuditDelta) -> str:
        sign = "+" if d.delta > 0 else ""
        return f"{label:22} {d.before:6} -> {d.after:6}  ({sign}{d.delta})"
    return "\n".join([
        "PPA Pilot Audit Comparison", "==========================", "",
        row("Files", c.total_files), row("Usable chronology", c.usable_chronology),
        row("Confirmed current", c.confirmed_current), row("Current proposals", c.proposed_current),
        row("Stale decisions", c.stale_decisions), row("Unresolved", c.unresolved),
        row("Conflicts", c.conflicts), row("Actionable review", c.actionable_review),
        row("Anchor questions", c.anchor_questions), row("Best anchor leverage", c.max_anchor_leverage),
    ])
