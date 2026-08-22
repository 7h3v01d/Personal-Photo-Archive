"""Phase 7.2.5 — controlled batch confirmation for reconstructed reset runs.

A batch is an authority boundary, not an inference engine. Eligibility is intentionally
strict: one strong-device reset run, every member reconstructed to a point date, every
stored row still PROPOSED and fresh, and only direct human-anchor/offset methods.
Planning is read-only. Commit revalidates the whole batch atomically before changing
any decision, so a single stale/changed member aborts the entire operation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from sqlite3 import Connection

from ppa.reconstruct_catalogue import _build_inputs, evaluate_staleness

BATCH_SCHEMA = "ppa-batch-confirmation/1"
_ALLOWED_METHODS = {"direct", "offset"}
_ALLOWED_CONFIDENCE = {"confirmed", "strong"}


@dataclass(frozen=True)
class BatchMember:
    file_id: str
    filename: str
    start_date: str
    method: str
    confidence: str
    source_revision_id: str
    evidence_fingerprint: str


@dataclass(frozen=True)
class BatchPlan:
    schema: str
    batch_id: str
    library_id: int
    members: tuple[BatchMember, ...]
    sample_file_ids: tuple[str, ...]
    anchor_file_ids: tuple[str, ...]
    day_offset: int | None
    reason: str

    @property
    def member_count(self) -> int:
        return len(self.members)

    def to_dict(self) -> dict:
        return asdict(self)


def _sample_ids(members: tuple[BatchMember, ...], limit: int = 5) -> tuple[str, ...]:
    n = len(members)
    if n <= limit:
        return tuple(m.file_id for m in members)
    # Deliberately sample the run edges and interior rather than first N.
    idxs = sorted({0, (n - 1) // 4, (n - 1) // 2, (3 * (n - 1)) // 4, n - 1})
    return tuple(members[i].file_id for i in idxs)


def _batch_id(members: tuple[BatchMember, ...]) -> str:
    payload = [
        {"file_id": m.file_id, "date": m.start_date,
         "revision": m.source_revision_id, "evidence": m.evidence_fingerprint}
        for m in members
    ]
    return "batch-" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]


def plan_batch_confirmation(conn: Connection, file_id: str) -> BatchPlan | None:
    """Return a strict, read-only batch plan for ``file_id`` or ``None``.

    The plan is available only when the complete strong reset group can be reviewed
    as one coherent point-date reconstruction. Partial/ambiguous/range/stale groups
    are intentionally ineligible.
    """
    inputs, _ = _build_inputs(conn)
    by_id = {i.file_id: i for i in inputs}
    target = by_id.get(file_id)
    if target is None or target.reset_group is None or not target.reset_group_strong:
        return None
    group = [i for i in inputs if i.reset_group == target.reset_group]
    if len(group) < 3 or not all(i.reset_group_strong for i in group):
        return None

    group_ids = {i.file_id for i in group}
    marks = ",".join("?" for _ in group_ids)
    rows = conn.execute(
        f"SELECT r.file_id, f.filename, f.library_id, r.start_date, r.end_date, "
        f"r.method, r.confidence, r.status, r.source_revision_id, r.evidence_fingerprint "
        f"FROM reconstructions r JOIN files f ON f.id=r.file_id "
        f"WHERE r.file_id IN ({marks})", tuple(sorted(group_ids))
    ).fetchall()
    if len(rows) != len(group_ids):
        return None
    libraries = {r["library_id"] for r in rows}
    if len(libraries) != 1:
        return None
    stale = evaluate_staleness(conn)
    if any(stale.get(r["file_id"], (True, True)) != (False, False) for r in rows):
        return None
    if any(r["status"] != "proposed" for r in rows):
        return None
    if any(r["end_date"] is not None for r in rows):
        return None
    if any(r["method"] not in _ALLOWED_METHODS for r in rows):
        return None
    if any(r["confidence"] not in _ALLOWED_CONFIDENCE for r in rows):
        return None
    if any(not r["source_revision_id"] or not r["evidence_fingerprint"] for r in rows):
        return None

    members = tuple(BatchMember(
        r["file_id"], r["filename"], r["start_date"], r["method"], r["confidence"],
        r["source_revision_id"], r["evidence_fingerprint"]
    ) for r in sorted(rows, key=lambda r: (r["filename"].casefold(), r["file_id"])))

    anchors = tuple(sorted(m.file_id for m in members if m.method == "direct"))
    # Strict batch requires one and only one exact human anchor basis. More than one
    # is safe in the pure engine if offsets agree, but batch authority is deliberately
    # narrower so the human can understand one clear provenance chain.
    if len(anchors) != 1:
        return None
    anchor_input = by_id[anchors[0]]
    if anchor_input.recorded is None or anchor_input.known_true is None:
        return None
    offset = (anchor_input.known_true - anchor_input.recorded.date()).days
    if offset == 0:
        return None

    reason = (f"{len(members)} fresh point-date proposals form one strong-device reset run, "
              f"all derived from one exact human anchor with a {offset:+d}-day clock offset. "
              "The batch will be revalidated atomically at commit time.")
    return BatchPlan(BATCH_SCHEMA, _batch_id(members), next(iter(libraries)), members,
                     _sample_ids(members), anchors, offset, reason)


def confirm_batch(conn: Connection, plan: BatchPlan) -> int:
    """Atomically confirm an unchanged batch plan.

    No partial confirmation is possible. The complete plan is rebuilt immediately
    before commit; any changed bytes, evidence, proposal, membership or date makes
    the plan token differ and aborts the operation.
    """
    if not plan.members:
        return 0
    current = plan_batch_confirmation(conn, plan.members[0].file_id)
    if current is None or current.batch_id != plan.batch_id:
        raise ValueError("batch changed since review; refresh the batch before confirming")
    expected = tuple(m.file_id for m in plan.members)
    if tuple(m.file_id for m in current.members) != expected:
        raise ValueError("batch membership changed since review; refresh before confirming")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("BEGIN")
        for m in current.members:
            cur = conn.execute(
                "UPDATE reconstructions SET status='confirmed', decided_at=? "
                "WHERE file_id=? AND status='proposed' AND source_revision_id=? "
                "AND evidence_fingerprint=?",
                (now, m.file_id, m.source_revision_id, m.evidence_fingerprint),
            )
            if cur.rowcount != 1:
                raise ValueError(f"batch member changed during commit: {m.filename}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(current.members)
