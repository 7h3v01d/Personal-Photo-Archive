"""Historical Date Reconstruction — Phase 7.1 (catalogue persistence & flow).

Runs the pure Phase 7 engine (``ppa.reconstruct``) against the catalogue, stores
proposals in the ``reconstructions`` table, and provides the confirm/reject flow
that turns a proposal into an authoritative interpretation.

Invariants (inherited, enforced here):

  * Read-of-evidence only. Reconstruction never writes to observations or the
    recorded date; it only writes to its own ``reconstructions`` table.
  * Human decisions are sticky. Re-running refreshes only 'proposed' rows and
    never overwrites a 'confirmed'/'rejected' decision.
  * Ownership/fail-closed anchor rules from Phase 6 are reused as-is (an anchor
    only applies within its owning library).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from sqlite3 import Connection

from ppa.reconstruct import (
    Confidence,
    KnownTrueKind,
    Reconstruction,
    ReconstructionInput,
    reconstruct,
)


@dataclass(frozen=True)
class StoredReconstruction:
    id: int
    file_id: str
    start_date: date
    end_date: date | None
    confidence: str
    method: str
    evidence: str | None
    status: str
    created_at: str
    decided_at: str | None
    source_revision_id: str | None
    engine_version: str | None
    updated_at: str | None
    evidence_fingerprint: str | None
    content_stale: bool   # the file's current revision differs from the bound one
    evidence_stale: bool  # today's evidence differs from what produced this row

    @property
    def stale(self) -> bool:
        return self.content_stale or self.evidence_stale


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _build_inputs(conn: Connection, camera_floors=None) -> tuple[list, dict]:
    """Assemble ReconstructionInput per present file, plus the Phase-6 final
    reliability map. Reuses the same evidence gathering as reconciliation."""
    from ppa import anchors as anchors_mod
    from ppa.chronology import _is_strong_serial, filename_sequence
    from ppa.chronology import analyse_library as _chron
    from ppa.reconcile import _parse_gps_datestamp, analyse_library_reconciled

    findings, chron = _chron(conn)
    _, final = analyse_library_reconciled(conn, camera_floors=camera_floors)

    strong_by_file = {
        r["id"]: _is_strong_serial(r["serial"])
        for r in conn.execute(
            "SELECT f.id, c.serial FROM files f LEFT JOIN cameras c ON c.id = f.camera_id "
            "WHERE f.presence_status = 'present'")
    }
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
        "SELECT f.id, f.relative_path, f.filename, f.library_id "
        "FROM files f WHERE f.presence_status = 'present'").fetchall()

    inputs: list[ReconstructionInput] = []
    for r in rows:
        fid = r["id"]
        pc = chron.get(fid)
        if pc is None:
            continue
        rel = (r["relative_path"] or r["filename"]).replace("\\", "/")
        directory = rel.rsplit("/", 1)[0] if "/" in rel else ""
        seq, _ = filename_sequence(r["filename"])
        reliability = final[fid].reliability if fid in final else pc.reliability

        known_true = None
        known_true_kind = None
        anchor_range = None
        anchor = anchors_mod.resolve_for(anchor_list, file_id=fid,
                                         directory=directory, library_id=r["library_id"])
        if anchor is not None and anchor.kind == "exact":
            known_true, known_true_kind = anchor.start_date, KnownTrueKind.HUMAN_EXACT
        elif anchor is not None:  # range
            anchor_range = (anchor.start_date, anchor.end_date)
        else:
            gps_row = conn.execute(
                "SELECT value FROM metadata_observations WHERE file_id = ? "
                "AND source = 'exif-gps' AND key = 'GPSDateStamp' AND file_revision_id = "
                "(SELECT current_revision_id FROM files WHERE id = ?)",
                (fid, fid)).fetchone()
            gps = _parse_gps_datestamp(gps_row["value"]) if gps_row else None
            if gps is not None:
                known_true, known_true_kind = gps.date(), KnownTrueKind.GPS_DATE

        inputs.append(ReconstructionInput(
            file_id=fid, recorded=pc.candidate, reliability=reliability,
            reset_group=reset_group.get(fid),
            reset_group_strong=reset_group_strong.get(fid, False), seq=seq,
            known_true=known_true, known_true_kind=known_true_kind,
            anchor_range=anchor_range))
    return inputs, final


def analyse_library_reconstructed(conn: Connection, *, camera_floors=None
                                  ) -> dict[str, Reconstruction]:
    """Run the pure reconstruction engine over the catalogue (no writes)."""
    inputs, _ = _build_inputs(conn, camera_floors)
    return reconstruct(inputs)


def _canon_input(i) -> dict:
    """Canonical, JSON-serialisable view of the COMPLETE semantic input to the
    reconstruction engine for one frame. Every ReconstructionInput field that can
    change ``reconstruct()``'s output is included — notably ``recorded``, since a
    re-extraction or parser fix can change the interpreted capture instant for the
    same bytes and offset propagation is ``recorded + offset``. (The bytes are the
    revision dimension; the interpreted observation extracted from them is
    evidence.)

    The ephemeral ``reset_group`` LABEL (``reset-0``, ``reset-1``…) is deliberately
    excluded: it's an enumeration artefact, so an unrelated earlier group could
    renumber it without any real change. Group participation is instead carried by
    the sorted group-member payload in ``_fingerprints`` — that captures which
    frames actually belong together, which is the semantic fact.
    """
    return {
        "file_id": i.file_id,
        "recorded": i.recorded.isoformat() if i.recorded else None,
        "reliability": i.reliability.value,
        "seq": i.seq,
        "reset_group_strong": i.reset_group_strong,
        "known_true": i.known_true.isoformat() if i.known_true else None,
        "known_true_kind": i.known_true_kind.value if i.known_true_kind else None,
        "anchor_range": ([i.anchor_range[0].isoformat(), i.anchor_range[1].isoformat()]
                         if i.anchor_range else None),
    }


def _fingerprints(inputs: list) -> dict[str, str]:
    """Deterministic SHA-256 of the evidence that determines each file's
    reconstruction. Offset propagation makes a frame depend on its reset group's
    members, so a grouped frame's payload includes the whole group — a change to
    any member's anchor/GPS re-fingerprints the whole run."""
    from ppa.reconstruct import ENGINE_VERSION

    by_group: dict[str, list] = {}
    for i in inputs:
        if i.reset_group is not None:
            by_group.setdefault(i.reset_group, []).append(i)

    out: dict[str, str] = {}
    for i in inputs:
        group = (sorted((_canon_input(m) for m in by_group[i.reset_group]),
                        key=lambda d: d["file_id"])
                 if i.reset_group is not None else None)
        payload = {"engine": ENGINE_VERSION, "self": _canon_input(i), "group": group}
        out[i.file_id] = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return out


def evaluate_staleness(conn: Connection, *, camera_floors=None
                       ) -> dict[str, tuple[bool, bool]]:
    """Recompute today's evidence and return {file_id: (content_stale,
    evidence_stale)} for every stored reconstruction. content_stale = the file's
    current revision differs from the bound one; evidence_stale = today's evidence
    fingerprint differs from the one frozen on the row."""
    inputs, _ = _build_inputs(conn, camera_floors)
    current_fp = _fingerprints(inputs)
    current_rev = {
        r["id"]: r["current_revision_id"]
        for r in conn.execute("SELECT id, current_revision_id FROM files")}
    out: dict[str, tuple[bool, bool]] = {}
    for r in conn.execute(
            "SELECT file_id, source_revision_id, evidence_fingerprint "
            "FROM reconstructions").fetchall():
        fid = r["file_id"]
        content_stale = r["source_revision_id"] != current_rev.get(fid)
        evidence_stale = (r["evidence_fingerprint"] is None
                          or r["evidence_fingerprint"] != current_fp.get(fid))
        out[fid] = (content_stale, evidence_stale)
    return out


def store_reconstructions(conn: Connection, *, camera_floors=None) -> dict[str, int]:
    """Compute reconstructions and persist proposals. Sticky: only 'proposed'
    rows are refreshed; 'confirmed'/'rejected' decisions are left untouched.

    Each proposal is bound to the file's CURRENT revision (source_revision_id) and
    the engine version, so a later revision change makes the row recognisably
    stale rather than silently authoritative over new bytes. created_at is
    preserved across recompute; updated_at tracks the last recompute.
    """
    from ppa.reconstruct import ENGINE_VERSION

    results = analyse_library_reconstructed(conn, camera_floors=camera_floors)
    inputs, _ = _build_inputs(conn, camera_floors)
    fingerprints = _fingerprints(inputs)
    current_rev = {
        r["id"]: r["current_revision_id"]
        for r in conn.execute("SELECT id, current_revision_id FROM files")}
    decided = {r["file_id"] for r in conn.execute(
        "SELECT file_id FROM reconstructions WHERE status IN ('confirmed','rejected')")}
    now = datetime.now(timezone.utc).isoformat()
    proposed = skipped = cleared = 0
    try:
        conn.execute("BEGIN")
        for row in conn.execute(
                "SELECT file_id FROM reconstructions WHERE status = 'proposed'").fetchall():
            if row["file_id"] not in results:
                conn.execute("DELETE FROM reconstructions WHERE file_id = ? "
                             "AND status = 'proposed'", (row["file_id"],))
                cleared += 1
        for fid, rec in results.items():
            if fid in decided:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO reconstructions (file_id, start_date, end_date, confidence, "
                "method, evidence, status, created_at, updated_at, source_revision_id, "
                "engine_version, evidence_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?) "
                "ON CONFLICT(file_id) DO UPDATE SET "
                "start_date=excluded.start_date, end_date=excluded.end_date, "
                "confidence=excluded.confidence, method=excluded.method, "
                "evidence=excluded.evidence, updated_at=excluded.updated_at, "
                "source_revision_id=excluded.source_revision_id, "
                "engine_version=excluded.engine_version, "
                "evidence_fingerprint=excluded.evidence_fingerprint "
                "WHERE reconstructions.status = 'proposed'",   # created_at preserved
                (fid, rec.start.isoformat(),
                 rec.end.isoformat() if rec.end else None,
                 rec.confidence.value, rec.method, rec.evidence, now, now,
                 current_rev.get(fid), ENGINE_VERSION, fingerprints.get(fid)))
            proposed += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"proposed": proposed, "skipped_decided": skipped, "cleared": cleared}


def list_reconstructions(conn: Connection, *, status: str | None = None,
                         camera_floors=None) -> list[StoredReconstruction]:
    """Stored reconstructions, with staleness derived against today's revision AND
    evidence. A decision made against superseded bytes (content_stale) or against
    superseded evidence like a since-changed anchor (evidence_stale) is stale."""
    staleness = evaluate_staleness(conn, camera_floors=camera_floors)
    sql = ("SELECT id, file_id, start_date, end_date, confidence, method, evidence, "
           "status, created_at, decided_at, source_revision_id, engine_version, "
           "updated_at, evidence_fingerprint FROM reconstructions")
    params: tuple = ()
    if status is not None:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY status, file_id"
    out: list[StoredReconstruction] = []
    for r in conn.execute(sql, params).fetchall():
        content_stale, evidence_stale = staleness.get(r["file_id"], (True, True))
        out.append(StoredReconstruction(
            r["id"], r["file_id"], _parse_date(r["start_date"]),
            _parse_date(r["end_date"]) if r["end_date"] else None,
            r["confidence"], r["method"], r["evidence"], r["status"],
            r["created_at"], r["decided_at"], r["source_revision_id"],
            r["engine_version"], r["updated_at"], r["evidence_fingerprint"],
            content_stale, evidence_stale))
    return out


def _decide(conn: Connection, file_id: str, status: str, camera_floors=None) -> bool:
    """Confirm or reject — allowed ONLY from 'proposed', and ONLY when the proposal
    is fresh against BOTH the current revision and the current evidence. A stale
    proposal (bytes OR evidence changed since it was generated) must be refreshed
    by re-running reconstruction first. Decisions are terminal; use
    ``reopen_reconstruction`` to revisit one. Raises ValueError on a disallowed
    transition or a stale proposal; returns False if there is no row."""
    row = conn.execute("SELECT status FROM reconstructions WHERE file_id = ?",
                       (file_id,)).fetchone()
    if row is None:
        return False
    if row["status"] != "proposed":
        raise ValueError(
            f"cannot mark as {status}: reconstruction is already '{row['status']}' "
            "(reopen it first to revisit the decision)")
    content_stale, evidence_stale = evaluate_staleness(
        conn, camera_floors=camera_floors).get(file_id, (True, True))
    if content_stale:
        raise ValueError(
            f"cannot mark as {status}: the file's bytes changed since this proposal "
            "was generated; re-run reconstruction to refresh it first")
    if evidence_stale:
        raise ValueError(
            f"cannot mark as {status}: the evidence (e.g. an anchor) changed since "
            "this proposal was generated; re-run reconstruction to refresh it first")
    conn.execute(
        "UPDATE reconstructions SET status = ?, decided_at = ? WHERE file_id = ?",
        (status, datetime.now(timezone.utc).isoformat(), file_id))
    conn.commit()
    return True


def confirm_reconstruction(conn: Connection, file_id: str, *, camera_floors=None) -> bool:
    """Mark a file's reconstruction authoritative for its CURRENT revision and
    evidence. Does NOT touch observations or the recorded date. Terminal; refused
    if the proposal is content- or evidence-stale."""
    return _decide(conn, file_id, "confirmed", camera_floors)


def reject_reconstruction(conn: Connection, file_id: str, *, camera_floors=None) -> bool:
    """Reject a file's reconstruction. Terminal; refused if stale."""
    return _decide(conn, file_id, "rejected", camera_floors)


def reopen_reconstruction(conn: Connection, file_id: str) -> bool:
    """Return a confirmed/rejected reconstruction to 'proposed' so it can be
    revisited (e.g. after the bytes changed and the old decision went stale). The
    next ``store_reconstructions`` rebinds it to the current revision. Returns
    False if there is no decided row to reopen."""
    cur = conn.execute(
        "UPDATE reconstructions SET status = 'proposed', decided_at = NULL "
        "WHERE file_id = ? AND status IN ('confirmed','rejected')", (file_id,))
    conn.commit()
    return cur.rowcount > 0
