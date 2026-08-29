"""Phase 10.2 — forensic investigation of logical-Photo identity divergence.

Read-only.  The projection explains what PPA actually observed about each
physical File/revision; it never splits/merges Photo identity or creates
lineage.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from sqlite3 import Connection

from ppa.current_identity import verified_current_sha256_sql

DIVERGENCE_INVESTIGATION_SCHEMA = "ppa-identity-divergence-investigation/2"
MODIFIED_IN_PLACE = "modified_in_place"
DISTINCT_WHEN_FIRST_OBSERVED = "distinct_when_first_observed"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class RevisionObservation:
    revision_id: str
    sha256: str | None
    size_bytes: int | None
    fs_mtime: str | None
    first_observed_at: str
    superseded_at: str | None
    observed_session: str | None
    is_current: bool


@dataclass(frozen=True)
class DivergentFileEvidence:
    file_id: str
    filename: str
    path: str
    presence_status: str
    health_status: str
    first_seen_at: str
    last_seen_at: str
    expected_sha256: str | None
    current_sha256: str | None
    current_revision_id: str | None
    revisions: tuple[RevisionObservation, ...]

    @property
    def known_revision_hashes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(r.sha256 for r in self.revisions if r.sha256))

    @property
    def modified_in_place(self) -> bool:
        return len(set(self.known_revision_hashes)) > 1


@dataclass(frozen=True)
class DivergenceInvestigation:
    schema: str
    library_id: int
    photo_id: str
    classification: str
    rationale: str
    files: tuple[DivergentFileEvidence, ...]

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "library_id": self.library_id,
            "photo_id": self.photo_id,
            "classification": self.classification,
            "rationale": self.rationale,
            "files": [
                {
                    "file_id": f.file_id,
                    "filename": f.filename,
                    "path": f.path,
                    "presence_status": f.presence_status,
                    "health_status": f.health_status,
                    "first_seen_at": f.first_seen_at,
                    "last_seen_at": f.last_seen_at,
                    "expected_sha256": f.expected_sha256,
                    "verified_current_sha256": f.current_sha256,
                    "current_sha256": f.current_sha256,
                    "current_revision_id": f.current_revision_id,
                    "modified_in_place": f.modified_in_place,
                    "revisions": [r.__dict__ for r in f.revisions],
                } for f in self.files
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def investigate_identity_divergence(conn: Connection, *, library_id: int, photo_id: str) -> DivergenceInvestigation:
    """Build a bounded, read-only forensic view for one divergent logical Photo."""
    before = conn.total_changes
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    verified_expr = verified_current_sha256_sql("f", "r")
    rows = conn.execute(
        f"""SELECT f.id,f.filename,f.path,f.presence_status,f.health_status,
                   f.first_seen_at,f.last_seen_at,f.sha256 AS expected_sha256,
                   f.current_revision_id,{verified_expr} AS verified_current_sha256
              FROM files f
              LEFT JOIN file_revisions r ON r.id=f.current_revision_id
             WHERE f.library_id=? AND f.photo_id=?
             ORDER BY f.first_seen_at,f.filename COLLATE NOCASE,f.id""",
        (library_id, photo_id)).fetchall()
    if not rows:
        raise ValueError("logical Photo is not represented in this Library")
    current_hashes = {r["verified_current_sha256"] for r in rows if r["verified_current_sha256"]}
    if len(current_hashes) < 2:
        raise ValueError("logical Photo does not currently have a known hash divergence")

    file_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in file_ids)
    rev_rows = conn.execute(
        f"""SELECT id,file_id,sha256,size_bytes,fs_mtime,first_observed_at,
                    superseded_at,observed_session
               FROM file_revisions WHERE file_id IN ({placeholders})
              ORDER BY file_id,first_observed_at,id""", tuple(file_ids)).fetchall()
    by_file: dict[str, list] = {fid: [] for fid in file_ids}
    current_by_file = {r["id"]: r["current_revision_id"] for r in rows}
    for rr in rev_rows:
        by_file[rr["file_id"]].append(RevisionObservation(
            rr["id"], rr["sha256"], rr["size_bytes"], rr["fs_mtime"], rr["first_observed_at"],
            rr["superseded_at"], rr["observed_session"], rr["id"] == current_by_file[rr["file_id"]]))

    files = tuple(DivergentFileEvidence(
        r["id"], r["filename"], r["path"], r["presence_status"], r["health_status"],
        r["first_seen_at"], r["last_seen_at"], r["expected_sha256"],
        r["verified_current_sha256"], r["current_revision_id"],
        tuple(by_file[r["id"]])) for r in rows)

    changed = [f for f in files if f.modified_in_place]
    if changed:
        classification = MODIFIED_IN_PLACE
        names = ", ".join(f.filename for f in changed)
        rationale = f"PPA observed more than one distinct revision hash for: {names}. This proves those physical File records changed bytes in place."
    else:
        fully_observed = all(f.revisions and f.revisions[0].sha256 for f in files if f.current_sha256)
        first_hashes = {f.revisions[0].sha256 for f in files if f.revisions and f.revisions[0].sha256}
        if fully_observed and len(first_hashes) >= 2:
            classification = DISTINCT_WHEN_FIRST_OBSERVED
            rationale = ("PPA first observed these physical Files with different known hashes and has no revision history proving an in-place change. "
                         "This does not establish derivation, originality, or whether the logical Photo should be split.")
        else:
            classification = INSUFFICIENT_EVIDENCE
            rationale = "Current hashes diverge, but revision history is incomplete; PPA cannot explain how the divergence arose."

    assert conn.total_changes == before, "divergence investigation must remain read-only"
    return DivergenceInvestigation(DIVERGENCE_INVESTIGATION_SCHEMA, library_id, photo_id, classification, rationale, files)
