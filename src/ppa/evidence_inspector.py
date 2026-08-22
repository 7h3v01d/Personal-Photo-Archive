"""Phase 7.2.4 — read-only historical-date evidence inspector.

Builds a structured explanation of the accepted Phase-6/7 reasoning for one
catalogued file.  It never creates evidence, changes a reconstruction, or writes
to the catalogue.  The UI/CLI can therefore answer "Why?" without duplicating
chronology rules or treating presentation prose as machine-readable provenance.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import json
from sqlite3 import Connection

from ppa.reconstruct import KnownTrueKind, ReconstructionInput, reconstruct
from ppa.reconstruct_catalogue import _build_inputs, evaluate_staleness, file_date_summary
from ppa.reconcile import analyse_library_reconciled
from ppa.chronology import analyse_library as analyse_chronology


@dataclass(frozen=True)
class EvidenceMember:
    file_id: str
    filename: str
    recorded: str | None
    reliability: str
    seq: int | None
    independent_evidence: str | None


@dataclass(frozen=True)
class EvidenceTrace:
    file_id: str
    filename: str
    relative_path: str
    camera: str | None
    recorded: str | None
    reliability: str
    phase6_reasons: tuple[str, ...]
    evidence_conflicts: tuple[str, ...]
    chronology_findings: tuple[str, ...]
    independent_evidence: tuple[str, ...]
    reset_group_strong: bool
    reset_group_members: tuple[EvidenceMember, ...]
    reconstruction_status: str | None
    reconstruction_span: str | None
    reconstruction_confidence: str | None
    reconstruction_method: str | None
    reconstruction_explanation: str | None
    derivation: tuple[str, ...]
    content_stale: bool
    evidence_stale: bool

    @property
    def stale(self) -> bool:
        return self.content_stale or self.evidence_stale

    def to_json(self) -> str:
        return json.dumps({"schema": "ppa-evidence-trace/1", **asdict(self)},
                          sort_keys=True, indent=2)


def _independent(i: ReconstructionInput) -> tuple[str, ...]:
    out: list[str] = []
    if i.known_true is not None:
        if i.known_true_kind is KnownTrueKind.HUMAN_EXACT:
            out.append(f"Exact human date: {i.known_true.isoformat()}")
        elif i.known_true_kind is KnownTrueKind.GPS_DATE:
            out.append(f"GPS UTC date: {i.known_true.isoformat()}")
    if i.anchor_range is not None:
        out.append(f"Human date range: {i.anchor_range[0].isoformat()}…{i.anchor_range[1].isoformat()}")
    return tuple(out)


def _member_label(i: ReconstructionInput) -> str | None:
    ev = _independent(i)
    return "; ".join(ev) if ev else None


def inspect_date_evidence(conn: Connection, file_id: str, *, camera_floors=None) -> EvidenceTrace:
    """Return the current, read-only evidence trace for ``file_id``.

    Raises ValueError for a missing/non-present file rather than manufacturing an
    explanation for evidence that is no longer in the active catalogue.
    """
    row = conn.execute(
        "SELECT f.id, f.filename, f.relative_path, f.camera_id, c.make, c.model, c.serial "
        "FROM files f LEFT JOIN cameras c ON c.id=f.camera_id "
        "WHERE f.id=? AND f.presence_status='present'", (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"present file not found: {file_id}")

    inputs, _ = _build_inputs(conn, camera_floors)
    by_id = {i.file_id: i for i in inputs}
    target = by_id.get(file_id)
    if target is None:
        raise ValueError(f"no chronology input for file: {file_id}")

    findings, _chron = analyse_chronology(conn)
    _rf, final = analyse_library_reconciled(conn, camera_floors=camera_floors)
    fa = final.get(file_id)
    results = reconstruct(inputs)
    result = results.get(file_id)

    summary = file_date_summary(
        conn, file_id,
        staleness=evaluate_staleness(conn, camera_floors=camera_floors)
        if conn.execute("SELECT 1 FROM reconstructions LIMIT 1").fetchone() else {},
    )
    stored = summary.reconstruction

    matched_findings = tuple(
        f"{f.kind}: {f.detail}" for f in findings if file_id in f.file_ids
    )

    group = []
    if target.reset_group is not None:
        group = sorted((i for i in inputs if i.reset_group == target.reset_group),
                       key=lambda i: (i.seq is None, i.seq or 0, i.file_id))
    names = {r["id"]: r["filename"] for r in conn.execute(
        "SELECT id, filename FROM files WHERE presence_status='present'").fetchall()}
    members = tuple(EvidenceMember(
        i.file_id, names.get(i.file_id, i.file_id),
        i.recorded.isoformat() if i.recorded else None,
        i.reliability.value, i.seq, _member_label(i)) for i in group)

    derivation: list[str] = []
    if result is not None:
        if result.method == "direct":
            derivation.append("This frame has an exact human/local calendar anchor; no propagation was needed.")
        elif result.method == "direct_gps":
            derivation.append("GPSDateStamp is UTC/date-only, so Phase 7 preserves ±1 day of local-date uncertainty.")
        elif result.method == "anchor_range":
            derivation.append("A human-provided date window is preserved as a range; Phase 7 does not invent point precision.")
        elif result.method == "offset":
            bases = []
            offsets = set()
            for m in group:
                if (m.known_true is not None and m.known_true_kind is KnownTrueKind.HUMAN_EXACT
                        and m.recorded is not None):
                    off = (m.known_true - m.recorded.date()).days
                    offsets.add(off)
                    bases.append((m, off))
            if len(offsets) == 1 and bases:
                off = next(iter(offsets))
                basis_names = ", ".join(f"{names.get(m.file_id, m.file_id)} ({m.file_id})" for m, _ in bases)
                derivation.append(f"Exact human anchor(s) on {basis_names} establish a {off:+d}-day camera-clock offset.")
                if target.recorded is not None:
                    derivation.append(
                        f"Applying {off:+d} days to recorded {target.recorded.date().isoformat()} gives {result.start.isoformat()}.")
            derivation.append("Propagation is allowed only because every member of this reset run has credible single-device identity.")
        elif result.method == "bracket":
            # Reproduce the engine's point-neighbour basis without interpreting its prose.
            ordered = sorted((m for m in group if m.seq is not None), key=lambda m: m.seq)
            points = [(m.seq, results[m.file_id].start, m.file_id) for m in ordered
                      if m.file_id in results and results[m.file_id].end is None]
            earlier = [(s, d, fid) for s, d, fid in points if s < target.seq]
            later = [(s, d, fid) for s, d, fid in points if s > target.seq]
            if earlier and later:
                left = max(earlier, key=lambda x: x[1])
                right = min(later, key=lambda x: x[1])
                derivation.append(
                    f"Strong same-device filename order brackets this frame between "
                    f"{names.get(left[2], left[2])}={left[1].isoformat()} and "
                    f"{names.get(right[2], right[2])}={right[1].isoformat()}.")

    camera_bits = [x for x in (row["make"], row["model"]) if x]
    camera = " ".join(camera_bits) or None
    if row["serial"]:
        camera = f"{camera or 'Camera'} · serial {row['serial']}"

    if stored is not None:
        span = (stored.start_date.isoformat() if stored.end_date is None else
                f"{stored.start_date.isoformat()}…{stored.end_date.isoformat()}")
        status, confidence, method, expl = (stored.status, stored.confidence,
                                            stored.method, stored.evidence)
        content_stale, evidence_stale = stored.content_stale, stored.evidence_stale
    elif result is not None:
        span = result.start.isoformat() if result.end is None else f"{result.start.isoformat()}…{result.end.isoformat()}"
        status, confidence, method, expl = ("current-unpersisted", result.confidence.value,
                                            result.method, result.evidence)
        content_stale = evidence_stale = False
    else:
        span = status = confidence = method = expl = None
        content_stale = evidence_stale = False

    return EvidenceTrace(
        file_id=file_id,
        filename=row["filename"],
        relative_path=row["relative_path"] or row["filename"],
        camera=camera,
        recorded=target.recorded.isoformat() if target.recorded else None,
        reliability=(fa.reliability.value if fa else target.reliability.value),
        phase6_reasons=tuple(fa.reasons if fa else ()),
        evidence_conflicts=tuple(fa.evidence_conflicts if fa else ()),
        chronology_findings=matched_findings,
        independent_evidence=_independent(target),
        reset_group_strong=target.reset_group_strong,
        reset_group_members=members,
        reconstruction_status=status,
        reconstruction_span=span,
        reconstruction_confidence=confidence,
        reconstruction_method=method,
        reconstruction_explanation=expl,
        derivation=tuple(derivation),
        content_stale=content_stale,
        evidence_stale=evidence_stale,
    )


def concise_text(t: EvidenceTrace) -> str:
    lines = [
        f"Why this date? — {t.filename}",
        "=" * (17 + len(t.filename)),
        f"File: {t.relative_path}",
        f"Camera: {t.camera or '-'}",
        f"Recorded: {t.recorded or '-'}",
        f"Phase-6 reliability: {t.reliability}",
    ]
    if t.phase6_reasons:
        lines += ["", "Reliability reasons:"] + [f"  • {x}" for x in t.phase6_reasons]
    if t.chronology_findings:
        lines += ["", "Cross-photo chronology:"] + [f"  • {x}" for x in t.chronology_findings]
    if t.independent_evidence:
        lines += ["", "Independent evidence:"] + [f"  • {x}" for x in t.independent_evidence]
    if t.evidence_conflicts:
        lines += ["", "Evidence conflicts:"] + [f"  • {x}" for x in t.evidence_conflicts]
    if t.reset_group_members:
        strength = "strong single-device" if t.reset_group_strong else "ambiguous/model-only"
        lines += ["", f"Reset run: {len(t.reset_group_members)} frame(s), {strength}"]
        for m in t.reset_group_members:
            ev = f"; {m.independent_evidence}" if m.independent_evidence else ""
            lines.append(f"  • {m.filename}  seq={m.seq if m.seq is not None else '-'}  recorded={m.recorded or '-'}{ev}")
    if t.reconstruction_span:
        stale = []
        if t.content_stale: stale.append("photo changed")
        if t.evidence_stale: stale.append("evidence changed")
        lines += ["", "Reconstruction:",
                  f"  Status: {t.reconstruction_status}",
                  f"  Result: {t.reconstruction_span}",
                  f"  Confidence: {t.reconstruction_confidence}",
                  f"  Method: {t.reconstruction_method}"]
        if stale:
            lines.append(f"  STALE: {', '.join(stale)}")
        if t.reconstruction_explanation:
            lines.append(f"  Engine explanation: {t.reconstruction_explanation}")
    if t.derivation:
        lines += ["", "Derivation:"] + [f"  • {x}" for x in t.derivation]
    lines += ["", "Read-only explanation; no evidence, decision, metadata, or source photo was modified."]
    return "\n".join(lines)
