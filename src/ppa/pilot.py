"""Phase 7.2.1 — read-only pilot analysis/reporting.

This module adds *no new chronology inference*.  It aggregates the accepted
Archive Core, Phase 6 and Phase 7 read models into a traceable collection-level
report so a real historical subset can be inspected before building the review
queue.

Every aggregate carries file ids (or group members) so a headline count can be
traced back to the exact catalogue records that produced it.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Callable, Collection

from ppa.chronology import _is_strong_serial, analyse_library as analyse_chronology
from ppa.reconcile import EvidenceEffect, analyse_library_reconciled
from ppa.reconstruct_catalogue import list_reconstructions

REPORT_SCHEMA = "ppa-pilot-report/1"


class PilotAnalysisCancelled(RuntimeError):
    """Raised when a caller cancels a long-running read-only pilot analysis."""


def _checkpoint(progress_cb: Callable[[str], None] | None,
                cancel_cb: Callable[[], bool] | None, message: str) -> None:
    if cancel_cb is not None and cancel_cb():
        raise PilotAnalysisCancelled()
    if progress_cb is not None:
        progress_cb(message)


@dataclass(frozen=True)
class CountBucket:
    count: int
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class CameraSummary:
    camera_id: int | None
    make: str | None
    model: str | None
    serial: str | None
    identity_strength: str
    file_count: int
    file_ids: tuple[str, ...]
    reliability: dict[str, int]
    reset_pattern_count: int
    order_conflict_count: int
    reconstruction_count: int
    confirmed_count: int


@dataclass(frozen=True)
class ResetGroupSummary:
    group_key: str
    file_ids: tuple[str, ...]
    file_count: int
    camera_id: int | None
    identity_strength: str
    recorded_start: str | None
    recorded_end: str | None
    independently_supported: int
    independently_contradicted: int
    reconstruction_count: int
    confirmed_count: int
    state: str


@dataclass(frozen=True)
class ConflictSummary:
    kind: str
    count: int
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class AnchorOpportunity:
    file_id: str
    affected_file_ids: tuple[str, ...]
    affected_count: int
    reason: str
    priority: str


@dataclass(frozen=True)
class PilotScope:
    library_id: int
    library_root: str
    directory_prefix: str | None
    explicit_file_ids: tuple[str, ...] | None


@dataclass(frozen=True)
class PilotReport:
    schema: str
    generated_at: str
    read_only: bool
    scope: PilotScope
    total_files: int
    total_photos: int
    reliability: dict[str, CountBucket]
    reconstruction: dict[str, CountBucket]
    conflicts: tuple[ConflictSummary, ...]
    cameras: tuple[CameraSummary, ...]
    reset_groups: tuple[ResetGroupSummary, ...]
    anchor_opportunities: tuple[AnchorOpportunity, ...]
    unresolved: dict[str, CountBucket]
    review_priority: dict[str, CountBucket]
    metadata_quality: dict[str, CountBucket]
    integrity: dict[str, CountBucket]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), indent=2 if pretty else None,
                          sort_keys=True, separators=None if pretty else (",", ":"))


def _scope_ids(conn: Connection, library_id: int, directory_prefix: str | None,
               file_ids: Collection[str] | None) -> tuple[set[str], PilotScope]:
    lib = conn.execute(
        "SELECT id, root_canonical_path FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()
    if lib is None:
        raise ValueError(f"unknown library id {library_id}")
    if directory_prefix is not None and file_ids is not None:
        raise ValueError("directory_prefix and file_ids are mutually exclusive")

    rows = conn.execute(
        "SELECT id, relative_path, filename FROM files "
        "WHERE library_id = ? AND presence_status = 'present'", (library_id,)
    ).fetchall()
    available = {r["id"] for r in rows}
    if file_ids is not None:
        wanted = set(file_ids)
        unknown = wanted - available
        if unknown:
            raise ValueError(f"file_ids are not present in library {library_id}: {sorted(unknown)}")
        selected = wanted
        explicit = tuple(sorted(wanted))
    elif directory_prefix is not None:
        prefix = directory_prefix.replace("\\", "/").strip("/")
        selected = set()
        for r in rows:
            rel = (r["relative_path"] or r["filename"]).replace("\\", "/").strip("/")
            if rel == prefix or rel.startswith(prefix + "/"):
                selected.add(r["id"])
        explicit = None
        directory_prefix = prefix
    else:
        selected = available
        explicit = None

    return selected, PilotScope(int(lib["id"]), lib["root_canonical_path"],
                                directory_prefix, explicit)


def _bucket(ids) -> CountBucket:
    vals = tuple(sorted(set(ids)))
    return CountBucket(len(vals), vals)


def analyse_pilot(conn: Connection, *, library_id: int,
                  directory_prefix: str | None = None,
                  file_ids: Collection[str] | None = None,
                  camera_floors=None,
                  generated_at: str | None = None,
                  progress_cb: Callable[[str], None] | None = None,
                  cancel_cb: Callable[[], bool] | None = None) -> PilotReport:
    """Build a deterministic, read-only analysis report for one pilot scope.

    ``progress_cb``/``cancel_cb`` are optional workflow hooks only; they do not
    alter analysis semantics or persistence.  They let the desktop UI keep a
    large real-library analysis visible and cancellable while it runs off-thread.
    """
    _checkpoint(progress_cb, cancel_cb, "Date Review: selecting library scope…")
    selected, scope = _scope_ids(conn, library_id, directory_prefix, file_ids)

    # Accepted engines only; no persistence calls.
    _checkpoint(progress_cb, cancel_cb, "Date Review: analysing chronology…")
    findings, chron = analyse_chronology(conn)
    _checkpoint(progress_cb, cancel_cb, "Date Review: reconciling date evidence…")
    _, final = analyse_library_reconciled(conn, camera_floors=camera_floors)
    _checkpoint(progress_cb, cancel_cb, "Date Review: checking reconstruction freshness…")
    stored = {r.file_id: r for r in list_reconstructions(conn, camera_floors=camera_floors)}
    _checkpoint(progress_cb, cancel_cb, "Date Review: assembling report…")

    rows = conn.execute(
        "SELECT f.id, f.photo_id, f.filename, f.relative_path, f.camera_id, "
        "f.health_status, c.make, c.model, c.serial "
        "FROM files f LEFT JOIN cameras c ON c.id=f.camera_id "
        "WHERE f.library_id=? AND f.presence_status='present'", (library_id,)
    ).fetchall()
    row_by = {r["id"]: r for r in rows if r["id"] in selected}

    # Reliability trace.
    reliability_ids: dict[str, list[str]] = defaultdict(list)
    for fid in sorted(selected):
        fa = final.get(fid)
        rating = fa.reliability.value if fa else "UNKNOWN"
        reliability_ids[rating].append(fid)
    reliability = {k: _bucket(reliability_ids.get(k, [])) for k in
                   ("TRUSTED", "PROBABLY_VALID", "QUESTIONABLE", "LIKELY_WRONG", "UNKNOWN")}

    # Reconstruction lifecycle, preserving freshness as a separate dimension.
    rec_ids: dict[str, list[str]] = defaultdict(list)
    for fid in sorted(selected):
        r = stored.get(fid)
        if r is None:
            rec_ids["none"].append(fid); continue
        suffix = "_stale" if r.stale else "_current"
        rec_ids[r.status + suffix].append(fid)
        if r.status == "proposed":
            rec_ids[f"proposed_{r.confidence}" + ("_stale" if r.stale else "_current")].append(fid)
    reconstruction = {k: _bucket(v) for k, v in sorted(rec_ids.items())}

    # Existing sequence findings; only report the portion inside scope, never leak ids.
    scoped_findings = []
    for f in findings:
        ids = tuple(fid for fid in f.file_ids if fid in selected)
        if ids:
            scoped_findings.append((f, ids))

    conflicts_map: dict[str, set[str]] = defaultdict(set)
    for f, ids in scoped_findings:
        if f.kind == "timestamp_order_conflict":
            conflicts_map["SEQUENCE_ORDER_CONFLICT"].update(ids)
    for fid in selected:
        fa = final.get(fid)
        if fa and fa.evidence_conflicts:
            conflicts_map["INDEPENDENT_EVIDENCE_CONFLICT"].add(fid)
        if fa and fa.evidence_effect is EvidenceEffect.CONTRADICT:
            conflicts_map["CALENDAR_CONTRADICTION"].add(fid)
        r = stored.get(fid)
        if r and r.stale:
            conflicts_map["STALE_HUMAN_DECISION"].add(fid)
    conflicts = tuple(ConflictSummary(k, len(v), tuple(sorted(v)))
                      for k, v in sorted(conflicts_map.items()))

    # Camera summaries.
    by_camera: dict[int | None, list] = defaultdict(list)
    for r in row_by.values():
        by_camera[r["camera_id"]].append(r)
    cameras: list[CameraSummary] = []
    for cid, members in by_camera.items():
        ids = {m["id"] for m in members}
        rel_counts = Counter((final.get(fid).reliability.value if final.get(fid) else "UNKNOWN")
                             for fid in ids)
        reset_count = sum(1 for f, sids in scoped_findings
                          if f.kind == "reset_pattern" and ids.intersection(sids))
        conflict_count = sum(1 for f, sids in scoped_findings
                             if f.kind == "timestamp_order_conflict" and ids.intersection(sids))
        first = members[0]
        strength = "DEVICE_STRONG" if _is_strong_serial(first["serial"]) else (
                   "MODEL_ONLY" if cid is not None else "UNKNOWN")
        rec_count = sum(1 for fid in ids if fid in stored)
        confirmed = sum(1 for fid in ids if fid in stored and stored[fid].status == "confirmed" and not stored[fid].stale)
        cameras.append(CameraSummary(cid, first["make"], first["model"], first["serial"], strength,
                                     len(ids), tuple(sorted(ids)), dict(sorted(rel_counts.items())),
                                     reset_count, conflict_count, rec_count, confirmed))
    cameras.sort(key=lambda c: (-c.file_count, c.make or "", c.model or "", str(c.camera_id)))

    # Reset-group summaries and first conservative leverage opportunities.
    reset_groups: list[ResetGroupSummary] = []
    opportunities: list[AnchorOpportunity] = []
    for idx, (f, ids_tuple) in enumerate((x for x in scoped_findings if x[0].kind == "reset_pattern")):
        ids = list(ids_tuple)
        strong = bool(ids) and all(_is_strong_serial(row_by[fid]["serial"]) for fid in ids if fid in row_by)
        camera_ids = {row_by[fid]["camera_id"] for fid in ids if fid in row_by}
        cid = next(iter(camera_ids)) if len(camera_ids) == 1 else None
        candidates = [chron[fid].candidate for fid in ids if fid in chron and chron[fid].candidate]
        support = sum(1 for fid in ids if final.get(fid) and final[fid].evidence_effect is EvidenceEffect.SUPPORT)
        contradict = sum(1 for fid in ids if final.get(fid) and final[fid].evidence_effect is EvidenceEffect.CONTRADICT)
        rc = sum(1 for fid in ids if fid in stored)
        conf = sum(1 for fid in ids if fid in stored and stored[fid].status == "confirmed" and not stored[fid].stale)
        if support and contradict:
            state = "EVIDENCE_CONFLICT"
        elif contradict and strong:
            state = "INDEPENDENTLY_CONTRADICTED"
        elif conf == len(ids) and ids:
            state = "CONFIRMED"
        elif rc:
            state = "PARTIALLY_RECONSTRUCTED"
        else:
            state = "PATTERN_ONLY"
        reset_groups.append(ResetGroupSummary(
            f"pilot-reset-{idx}", tuple(sorted(ids)), len(ids), cid,
            "DEVICE_STRONG" if strong else "MODEL_ONLY_OR_UNKNOWN",
            min(candidates).isoformat() if candidates else None,
            max(candidates).isoformat() if candidates else None,
            support, contradict, rc, conf, state))

        unresolved_ids = [fid for fid in ids
                          if (fid not in stored or stored[fid].status != "confirmed" or stored[fid].stale)
                          and final.get(fid) is not None
                          and final[fid].reliability.value in ("QUESTIONABLE", "LIKELY_WRONG", "UNKNOWN")]
        if strong and len(unresolved_ids) >= 2:
            # Pick the middle sequence member as a neutral review candidate; this is
            # leverage detection only, NOT a claim that the user knows its date.
            ordered = sorted(unresolved_ids,
                             key=lambda fid: (getattr(chron.get(fid), "candidate", None) is None,
                                              row_by[fid]["filename"] if fid in row_by else fid))
            candidate = ordered[len(ordered)//2]
            affected = tuple(sorted(fid for fid in unresolved_ids if fid != candidate))
            if affected:
                opportunities.append(AnchorOpportunity(
                    candidate, affected, len(affected),
                    f"Confirming one frame in this strong-device reset pattern could constrain "
                    f"up to {len(affected)} other unresolved frame(s).",
                    "A" if len(affected) >= 10 else "B"))

    # Metadata-quality counts from current observations. Fetch once for the
    # selected library rather than issuing one SQL query per photo (critical on
    # real 10k+ collections).
    _checkpoint(progress_cb, cancel_cb, "Date Review: summarising metadata quality…")
    meta_ids: dict[str, list[str]] = defaultdict(list)
    obs_by_file: dict[str, list] = defaultdict(list)
    if selected:
        for o in conn.execute(
            "SELECT m.file_id, m.source, m.key, m.value "
            "FROM metadata_observations m "
            "JOIN files f ON f.id=m.file_id AND f.current_revision_id=m.file_revision_id "
            "WHERE f.library_id=? AND f.presence_status='present'",
            (library_id,),
        ).fetchall():
            if o["file_id"] in selected:
                obs_by_file[o["file_id"]].append(o)
    for n, fid in enumerate(sorted(selected)):
        if n % 250 == 0 and cancel_cb is not None and cancel_cb():
            raise PilotAnalysisCancelled()
        obs = obs_by_file.get(fid, ())
        pairs = {(o["source"], o["key"]): o["value"] for o in obs}
        if ("exif", "DateTimeOriginal") not in pairs:
            meta_ids["NO_DATETIMEORIGINAL"].append(fid)
        if row_by.get(fid) is not None and row_by[fid]["camera_id"] is None:
            meta_ids["NO_IDENTIFIABLE_CAMERA"].append(fid)
        elif row_by.get(fid) is not None and not _is_strong_serial(row_by[fid]["serial"]):
            meta_ids["MODEL_ONLY_CAMERA_IDENTITY"].append(fid)
        if ("exif-gps", "GPSDateStamp") in pairs:
            meta_ids["GPS_AVAILABLE"].append(fid)
        if ("exif", "DateTimeOriginal") in pairs:
            from ppa.dating import DateObservation, assess
            a = assess([DateObservation("exif", "DateTimeOriginal", pairs[("exif", "DateTimeOriginal")])])
            if any("malformed" in reason.lower() for reason in a.reasons):
                meta_ids["MALFORMED_DATETIMEORIGINAL"].append(fid)
    metadata_quality = {k: _bucket(v) for k, v in sorted(meta_ids.items())}

    # Integrity current-state only.
    integrity_ids: dict[str, list[str]] = defaultdict(list)
    for fid, r in row_by.items():
        if r["health_status"] != "ok":
            integrity_ids[r["health_status"].upper()].append(fid)
    integrity = {k: _bucket(v) for k, v in sorted(integrity_ids.items())}

    # Unresolved reason: exactly one primary reason per unresolved file.
    unresolved_ids: dict[str, list[str]] = defaultdict(list)
    reset_members = {fid for g in reset_groups for fid in g.file_ids}
    for fid in sorted(selected):
        rec = stored.get(fid)
        if rec and rec.status == "confirmed" and not rec.stale:
            continue
        fa = final.get(fid)
        if rec and rec.stale:
            reason = "STALE_DECISION_NEEDS_REVIEW"
        elif fid in reset_members and fa and fa.reliability.value in ("QUESTIONABLE", "LIKELY_WRONG"):
            reason = "RESET_PATTERN_NEEDS_REVIEW"
        elif fa is None or fa.reliability.value == "UNKNOWN":
            reason = "NO_USABLE_DATE_EVIDENCE"
        elif fa.evidence_conflicts:
            reason = "CONFLICTING_INDEPENDENT_EVIDENCE"
        elif fa.reliability.value in ("QUESTIONABLE", "LIKELY_WRONG"):
            reason = "QUESTIONABLE_WITHOUT_RESOLUTION"
        elif rec is None:
            reason = "NO_RECONSTRUCTION"
        else:
            reason = "AWAITING_HUMAN_REVIEW"
        unresolved_ids[reason].append(fid)
    unresolved = {k: _bucket(v) for k, v in sorted(unresolved_ids.items())}

    # Review priority: one band per file. A = stale/conflict/high leverage/strong proposal.
    opportunity_by_file = {o.file_id: o for o in opportunities}
    conflict_files = {fid for c in conflicts for fid in c.file_ids}
    priority_ids: dict[str, list[str]] = defaultdict(list)
    for fid in sorted(selected):
        rec = stored.get(fid)
        fa = final.get(fid)
        if rec and rec.stale:
            p = "A"
        elif fid in conflict_files:
            p = "A"
        elif fid in opportunity_by_file and opportunity_by_file[fid].affected_count >= 10:
            p = "A"
        elif rec and rec.status == "proposed" and not rec.stale and rec.confidence == "strong":
            p = "A"
        elif rec and rec.status == "proposed" and not rec.stale:
            p = "B"
        elif fa and fa.reliability.value in ("QUESTIONABLE", "LIKELY_WRONG"):
            p = "C"
        else:
            p = "D"
        priority_ids[p].append(fid)
    review_priority = {k: _bucket(priority_ids.get(k, [])) for k in ("A", "B", "C", "D")}

    photo_ids = {row_by[fid]["photo_id"] for fid in selected if fid in row_by}
    return PilotReport(
        REPORT_SCHEMA,
        generated_at or datetime.now(timezone.utc).isoformat(),
        True, scope, len(selected), len(photo_ids), reliability, reconstruction,
        conflicts, tuple(cameras), tuple(reset_groups),
        tuple(sorted(opportunities, key=lambda o: (-o.affected_count, o.file_id))),
        unresolved, review_priority, metadata_quality, integrity)


def concise_text(report: PilotReport) -> str:
    """Compact human-readable CLI summary; structured report remains authoritative."""
    lines = ["PPA Pilot Analysis", "==================", "",
             f"Library: {report.scope.library_root}",
             f"Files analysed: {report.total_files}", f"Photos: {report.total_photos}", "",
             "Date reliability", "----------------"]
    for key in ("TRUSTED", "PROBABLY_VALID", "QUESTIONABLE", "LIKELY_WRONG", "UNKNOWN"):
        lines.append(f"{key:16} {report.reliability[key].count}")
    lines += ["", "Review priority", "---------------"]
    for key in ("A", "B", "C", "D"):
        lines.append(f"Priority {key:8} {report.review_priority[key].count}")
    lines += ["", f"Reset patterns: {len(report.reset_groups)}",
              f"Chronology/evidence conflicts: {sum(c.count for c in report.conflicts)}",
              f"High-leverage anchor opportunities: {len(report.anchor_opportunities)}",
              "", "(Read-only report; no photo, evidence, anchor, reconstruction, or decision was modified.)"]
    return "\n".join(lines)
