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


def store_reconstructions(conn: Connection, *, camera_floors=None) -> dict[str, int]:
    """Compute reconstructions and persist proposals. Sticky: only 'proposed'
    rows are refreshed; 'confirmed'/'rejected' decisions are left untouched.
    Returns counts: {'proposed': n, 'skipped_decided': m, 'cleared': k}."""
    results = analyse_library_reconstructed(conn, camera_floors=camera_floors)
    decided = {r["file_id"] for r in conn.execute(
        "SELECT file_id FROM reconstructions WHERE status IN ('confirmed','rejected')")}
    now = datetime.now(timezone.utc).isoformat()
    proposed = skipped = cleared = 0
    try:
        conn.execute("BEGIN")
        # Drop stale proposals for files that no longer reconstruct (leave
        # decisions alone).
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
                "method, evidence, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?) "
                "ON CONFLICT(file_id) DO UPDATE SET "
                "start_date=excluded.start_date, end_date=excluded.end_date, "
                "confidence=excluded.confidence, method=excluded.method, "
                "evidence=excluded.evidence, created_at=excluded.created_at "
                "WHERE reconstructions.status = 'proposed'",
                (fid, rec.start.isoformat(),
                 rec.end.isoformat() if rec.end else None,
                 rec.confidence.value, rec.method, rec.evidence, now))
            proposed += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"proposed": proposed, "skipped_decided": skipped, "cleared": cleared}


def list_reconstructions(conn: Connection, *, status: str | None = None
                         ) -> list[StoredReconstruction]:
    sql = ("SELECT id, file_id, start_date, end_date, confidence, method, evidence, "
           "status, created_at, decided_at FROM reconstructions")
    params: tuple = ()
    if status is not None:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY status, file_id"
    return [
        StoredReconstruction(
            r["id"], r["file_id"], _parse_date(r["start_date"]),
            _parse_date(r["end_date"]) if r["end_date"] else None,
            r["confidence"], r["method"], r["evidence"], r["status"],
            r["created_at"], r["decided_at"])
        for r in conn.execute(sql, params).fetchall()
    ]


def _decide(conn: Connection, file_id: str, status: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE reconstructions SET status = ?, decided_at = ? WHERE file_id = ?",
        (status, now, file_id))
    conn.commit()
    return cur.rowcount > 0


def confirm_reconstruction(conn: Connection, file_id: str) -> bool:
    """Mark a file's reconstruction authoritative. Does NOT touch observations or
    the recorded date. Returns True if a row was updated."""
    return _decide(conn, file_id, "confirmed")


def reject_reconstruction(conn: Connection, file_id: str) -> bool:
    """Reject a file's reconstruction (kept for audit, never resolved). Returns
    True if a row was updated."""
    return _decide(conn, file_id, "rejected")
