"""Phase 7.2.6 — explicit, read-only unresolved-memory classification.

Uncertainty is an archival state, not a failure.  This module classifies every
not-currently-confirmed photo in a selected library scope into exactly one
primary unresolved category while retaining traceable secondary facts.

No inference, anchor, reconstruction, decision, metadata, or source file is
modified here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from sqlite3 import Connection
from typing import Callable, Collection

from ppa import anchors as anchors_mod
from ppa.pilot import analyse_pilot
from ppa.reconstruct_catalogue import list_reconstructions

UNRESOLVED_SCHEMA = "ppa-unresolved-memories/1"

_CATEGORY_ORDER = {
    "STALE_DECISION_NEEDS_REVIEW": 0,
    "CONFLICTING_EVIDENCE": 1,
    "RESET_RUN_WITHOUT_EXACT_ANCHOR": 2,
    "RANGE_ONLY_KNOWLEDGE": 3,
    "AWAITING_HUMAN_REVIEW": 4,
    "QUESTIONABLE_WITHOUT_CORROBORATION": 5,
    "NO_RECONSTRUCTION": 6,
    "NO_USABLE_DATE_EVIDENCE": 7,
}

_CATEGORY_LABEL = {
    "STALE_DECISION_NEEDS_REVIEW": "Stale decision needs review",
    "CONFLICTING_EVIDENCE": "Conflicting evidence",
    "RESET_RUN_WITHOUT_EXACT_ANCHOR": "Reset run without an exact human anchor",
    "RANGE_ONLY_KNOWLEDGE": "Range-only date knowledge",
    "AWAITING_HUMAN_REVIEW": "Reconstruction awaiting human review",
    "QUESTIONABLE_WITHOUT_CORROBORATION": "Questionable date without corroboration",
    "NO_RECONSTRUCTION": "No reconstruction available",
    "NO_USABLE_DATE_EVIDENCE": "No usable date evidence",
}


@dataclass(frozen=True)
class UnresolvedMemory:
    file_id: str
    filename: str
    category: str
    label: str
    reason: str
    reliability: str
    reconstruction_status: str | None
    reconstruction_confidence: str | None
    stale: bool
    reset_group_size: int
    has_exact_anchor: bool
    has_range_anchor: bool
    related_file_ids: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedCategory:
    category: str
    label: str
    count: int
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedMemories:
    schema: str
    library_id: int
    total_files: int
    unresolved_count: int
    categories: tuple[UnresolvedCategory, ...]
    items: tuple[UnresolvedMemory, ...]
    read_only: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), indent=2 if pretty else None,
                          sort_keys=True, separators=None if pretty else (",", ":"))


def _effective_anchor_map(conn: Connection, report) -> dict[str, object | None]:
    anchors = anchors_mod.list_anchors(conn)
    rows = conn.execute(
        "SELECT id, relative_path, filename FROM files WHERE library_id=?",
        (report.scope.library_id,),
    ).fetchall()
    out: dict[str, object | None] = {}
    for row in rows:
        fid = row["id"]
        rel = (row["relative_path"] or row["filename"]).replace("\\", "/")
        directory = rel.rsplit("/", 1)[0] if "/" in rel else ""
        out[fid] = anchors_mod.resolve_for(
            anchors, file_id=fid, directory=directory, library_id=report.scope.library_id)
    return out


def build_unresolved_memories(conn: Connection, *, library_id: int,
                              directory_prefix: str | None = None,
                              file_ids: Collection[str] | None = None,
                              camera_floors=None,
                              progress_cb: Callable[[str], None] | None = None,
                              cancel_cb: Callable[[], bool] | None = None,
                              report=None) -> UnresolvedMemories:
    """Return a deterministic, read-only unresolved-memory view.

    Exactly one primary category is assigned to each photo without a fresh
    confirmed reconstruction.  Category precedence favours states requiring
    human attention (stale/conflicting) over merely low-information states.
    """
    if report is None:
        report = analyse_pilot(conn, library_id=library_id,
                               directory_prefix=directory_prefix, file_ids=file_ids,
                               camera_floors=camera_floors, generated_at="unresolved",
                               progress_cb=progress_cb, cancel_cb=cancel_cb)
    elif report.scope.library_id != library_id:
        raise ValueError("supplied pilot report belongs to a different library")
    recs = {r.file_id: r for r in list_reconstructions(conn, camera_floors=camera_floors)}
    selected = set().union(*(b.file_ids for b in report.reliability.values()))

    if selected:
        marks = ",".join("?" for _ in selected)
        rows = conn.execute(
            f"SELECT id, filename FROM files WHERE library_id=? AND id IN ({marks})",
            (library_id, *sorted(selected)),
        ).fetchall()
        names = {r["id"]: r["filename"] for r in rows}
    else:
        names = {}

    reliability: dict[str, str] = {}
    for rating, bucket in report.reliability.items():
        for fid in bucket.file_ids:
            reliability[fid] = rating

    no_capture_date = set(report.metadata_quality.get(
        "NO_DATETIMEORIGINAL", type("_Empty", (), {"file_ids": ()})()).file_ids)

    conflict_files = {fid for c in report.conflicts for fid in c.file_ids
                      if c.kind != "STALE_HUMAN_DECISION"}
    reset_by_file: dict[str, object] = {}
    for group in report.reset_groups:
        for fid in group.file_ids:
            reset_by_file[fid] = group

    anchor_by_file = _effective_anchor_map(conn, report)

    items: list[UnresolvedMemory] = []
    for fid in sorted(selected):
        rec = recs.get(fid)
        if rec is not None and rec.status == "confirmed" and not rec.stale:
            continue
        rel = reliability.get(fid, "UNKNOWN")
        # A clean recorded chronology is already usable history; Phase 7 exists
        # to reconstruct doubt, not to force every healthy photo through a human
        # confirmation ceremony.  Keep such photos out of Unresolved Memories.
        if rec is None and rel in ("TRUSTED", "PROBABLY_VALID") and fid not in conflict_files:
            continue
        anchor = anchor_by_file.get(fid)
        exact_anchor = bool(anchor is not None and getattr(anchor, "kind", None) == "exact")
        range_anchor = bool(anchor is not None and getattr(anchor, "kind", None) == "range")
        group = reset_by_file.get(fid)
        related = tuple(x for x in getattr(group, "file_ids", ()) if x != fid)
        group_has_exact = any(
            (anchor_by_file.get(other) is not None and
             getattr(anchor_by_file.get(other), "kind", None) == "exact")
            for other in getattr(group, "file_ids", ())) if group is not None else False

        if rec is not None and rec.stale:
            category = "STALE_DECISION_NEEDS_REVIEW"
            reason = "A stored reconstruction/decision no longer matches current bytes or evidence."
        elif fid in conflict_files:
            category = "CONFLICTING_EVIDENCE"
            reason = "Independent chronology evidence disagrees; PPA will not choose a winner automatically."
        elif group is not None and getattr(group, "identity_strength", "") == "DEVICE_STRONG" and not group_has_exact:
            category = "RESET_RUN_WITHOUT_EXACT_ANCHOR"
            reason = (f"This photo belongs to a strong-device reset run of {group.file_count} photos, "
                      "but the run has no exact human calendar anchor.")
        elif range_anchor or (rec is not None and rec.end_date is not None):
            category = "RANGE_ONLY_KNOWLEDGE"
            reason = "Available evidence supports a date range, not a defensible exact day."
        elif fid in no_capture_date and anchor is None and rec is None:
            category = "NO_USABLE_DATE_EVIDENCE"
            reason = ("No observed capture-date metadata is available; filesystem time alone "
                      "is not treated as a trustworthy photographic date.")
        elif rec is not None and rec.status == "proposed":
            category = "AWAITING_HUMAN_REVIEW"
            reason = "A current reconstruction exists but has not been confirmed by a human."
        elif rel in ("QUESTIONABLE", "LIKELY_WRONG"):
            category = "QUESTIONABLE_WITHOUT_CORROBORATION"
            reason = f"The recorded date is {rel.lower().replace('_', ' ')} and lacks enough corroboration to reconstruct safely."
        elif rel == "UNKNOWN":
            category = "NO_USABLE_DATE_EVIDENCE"
            reason = "No usable calendar date evidence is currently available."
        else:
            category = "NO_RECONSTRUCTION"
            reason = "The recorded chronology is not confirmed by Phase 7 and no reconstruction is currently available."

        items.append(UnresolvedMemory(
            fid, names.get(fid, fid), category, _CATEGORY_LABEL[category], reason, rel,
            rec.status if rec else None, rec.confidence if rec else None,
            bool(rec and rec.stale), getattr(group, "file_count", 0) if group else 0,
            exact_anchor, range_anchor, tuple(sorted(related))))

    if cancel_cb is not None and cancel_cb():
        from ppa.pilot import PilotAnalysisCancelled
        raise PilotAnalysisCancelled()
    if progress_cb is not None:
        progress_cb("Unresolved Memories: classifying uncertainty…")
    items.sort(key=lambda i: (_CATEGORY_ORDER[i.category], i.filename.casefold(), i.file_id))
    by_category: dict[str, list[str]] = {}
    for item in items:
        by_category.setdefault(item.category, []).append(item.file_id)
    categories = tuple(
        UnresolvedCategory(cat, _CATEGORY_LABEL[cat], len(ids), tuple(sorted(ids)))
        for cat, ids in sorted(by_category.items(), key=lambda kv: (_CATEGORY_ORDER[kv[0]], kv[0]))
    )
    return UnresolvedMemories(UNRESOLVED_SCHEMA, library_id, len(selected), len(items),
                              categories, tuple(items), True)


def concise_text(view: UnresolvedMemories) -> str:
    lines = ["PPA Unresolved Memories", "=======================", "",
             f"Library: {view.library_id}",
             f"Unresolved: {view.unresolved_count} / {view.total_files}", ""]
    if not view.items:
        lines.append("No unresolved date memories in this scope.")
        return "\n".join(lines)
    lines.append("Categories")
    lines.append("----------")
    for c in view.categories:
        lines.append(f"{c.count:5}  {c.label}")
    lines.extend(["", "Items", "-----"])
    for item in view.items:
        lines.append(f"[{item.label}] {item.filename}")
        lines.append(f"  {item.reason}")
    return "\n".join(lines)
