"""Phase 10.6 — forensic investigation of competing logical Photo identity.

Read-only. A competing identity exists when the same known current SHA-256 is
owned by Files attached to more than one logical Photo in one Library. This
module explains the catalogue/revision evidence and whether a future controlled
merge could even be considered. It never merges, rewrites, deletes, or infers
lineage.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from sqlite3 import Connection

COMPETING_IDENTITY_INVESTIGATION_SCHEMA = "ppa-competing-identity-investigation/1"
CONVERGED_AFTER_OBSERVED_CHANGE = "converged_after_observed_change"
BYTE_IDENTICAL_WHEN_FIRST_OBSERVED = "byte_identical_when_first_observed"
INSUFFICIENT_HISTORY = "insufficient_history"


@dataclass(frozen=True)
class CompetingRevisionEvidence:
    revision_id: str
    sha256: str | None
    first_observed_at: str
    superseded_at: str | None
    is_current: bool


@dataclass(frozen=True)
class CompetingFileEvidence:
    file_id: str
    photo_id: str
    library_id: int
    filename: str
    path: str
    presence_status: str
    health_status: str
    first_seen_at: str
    last_seen_at: str
    current_sha256: str | None
    current_revision_id: str | None
    revisions: tuple[CompetingRevisionEvidence, ...]

    @property
    def changed_to_shared_bytes(self) -> bool:
        known = [r.sha256 for r in self.revisions if r.sha256]
        return bool(self.current_sha256 and len(set(known)) > 1 and known[-1] == self.current_sha256)


@dataclass(frozen=True)
class CompetingPhotoEvidence:
    photo_id: str
    created_at: str
    files: tuple[CompetingFileEvidence, ...]


@dataclass(frozen=True)
class MergeConsideration:
    eligible: bool
    status: str
    blockers: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class CompetingIdentityInvestigation:
    schema: str
    library_id: int
    sha256: str
    classification: str
    rationale: str
    photos: tuple[CompetingPhotoEvidence, ...]
    merge_consideration: MergeConsideration

    @property
    def photo_ids(self) -> tuple[str, ...]:
        return tuple(p.photo_id for p in self.photos)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "library_id": self.library_id,
            "sha256": self.sha256,
            "classification": self.classification,
            "rationale": self.rationale,
            "photo_ids": list(self.photo_ids),
            "merge_consideration": {
                "eligible": self.merge_consideration.eligible,
                "status": self.merge_consideration.status,
                "blockers": list(self.merge_consideration.blockers),
                "rationale": self.merge_consideration.rationale,
            },
            "photos": [
                {
                    "photo_id": p.photo_id,
                    "created_at": p.created_at,
                    "files": [
                        {
                            "file_id": f.file_id,
                            "library_id": f.library_id,
                            "filename": f.filename,
                            "path": f.path,
                            "presence_status": f.presence_status,
                            "health_status": f.health_status,
                            "first_seen_at": f.first_seen_at,
                            "last_seen_at": f.last_seen_at,
                            "current_sha256": f.current_sha256,
                            "current_revision_id": f.current_revision_id,
                            "changed_to_shared_bytes": f.changed_to_shared_bytes,
                            "revisions": [r.__dict__ for r in f.revisions],
                        }
                        for f in p.files
                    ],
                }
                for p in self.photos
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def _in_clause(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def investigate_competing_identity(conn: Connection, *, library_id: int, sha256: str) -> CompetingIdentityInvestigation:
    """Explain one current byte-identical multi-Photo identity conflict.

    The result is a bounded, read-only forensic projection. ``merge_consideration``
    is only an eligibility screen for a future controlled workflow; it is not a
    merge decision or authority claim.
    """
    before = conn.total_changes
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    sha256 = str(sha256 or "").strip()
    if not sha256:
        raise ValueError("known current SHA-256 is required")

    owners = conn.execute(
        "SELECT DISTINCT photo_id FROM files WHERE library_id=? AND sha256=? ORDER BY photo_id",
        (library_id, sha256),
    ).fetchall()
    photo_ids = tuple(r["photo_id"] for r in owners)
    if len(photo_ids) < 2:
        raise ValueError("current SHA-256 is not assigned to multiple logical Photos in this Library")

    ph = _in_clause(photo_ids)
    photo_rows = conn.execute(
        f"SELECT id,created_at,notes FROM photos WHERE id IN ({ph}) ORDER BY id", photo_ids
    ).fetchall()
    file_rows = conn.execute(
        f"""SELECT id,photo_id,library_id,filename,path,presence_status,health_status,
                    first_seen_at,last_seen_at,sha256,current_revision_id
               FROM files WHERE photo_id IN ({ph})
              ORDER BY photo_id,library_id,first_seen_at,filename COLLATE NOCASE,id""",
        photo_ids,
    ).fetchall()
    file_ids = tuple(r["id"] for r in file_rows)
    if file_ids:
        fph = _in_clause(file_ids)
        rev_rows = conn.execute(
            f"""SELECT id,file_id,sha256,first_observed_at,superseded_at
                   FROM file_revisions WHERE file_id IN ({fph})
                  ORDER BY file_id,first_observed_at,id""",
            file_ids,
        ).fetchall()
    else:
        rev_rows = ()

    by_rev: dict[str, list[CompetingRevisionEvidence]] = {fid: [] for fid in file_ids}
    current_rev = {r["id"]: r["current_revision_id"] for r in file_rows}
    for rr in rev_rows:
        by_rev[rr["file_id"]].append(CompetingRevisionEvidence(
            rr["id"], rr["sha256"], rr["first_observed_at"], rr["superseded_at"],
            rr["id"] == current_rev.get(rr["file_id"]),
        ))

    by_photo_files: dict[str, list[CompetingFileEvidence]] = {pid: [] for pid in photo_ids}
    for r in file_rows:
        by_photo_files[r["photo_id"]].append(CompetingFileEvidence(
            r["id"], r["photo_id"], int(r["library_id"]), r["filename"], r["path"],
            r["presence_status"], r["health_status"], r["first_seen_at"], r["last_seen_at"],
            r["sha256"], r["current_revision_id"], tuple(by_rev[r["id"]]),
        ))
    created = {r["id"]: r["created_at"] for r in photo_rows}
    photos = tuple(CompetingPhotoEvidence(pid, created.get(pid, ""), tuple(by_photo_files[pid])) for pid in photo_ids)

    shared_current_files = [f for p in photos for f in p.files
                            if f.library_id == int(library_id) and f.current_sha256 == sha256]
    changed = [f for f in shared_current_files if f.changed_to_shared_bytes]
    if changed:
        classification = CONVERGED_AFTER_OBSERVED_CHANGE
        names = ", ".join(sorted(f.filename for f in changed))
        rationale = (
            f"PPA observed at least one physical File change from different bytes into the shared current SHA-256 ({names}). "
            "This explains convergence of bytes after observation, but does not decide which logical Photo identity is correct."
        )
    else:
        complete = bool(shared_current_files) and all(f.revisions and f.revisions[0].sha256 for f in shared_current_files)
        first_shared = complete and all(f.revisions[0].sha256 == sha256 for f in shared_current_files)
        if first_shared:
            classification = BYTE_IDENTICAL_WHEN_FIRST_OBSERVED
            rationale = (
                "PPA first observed the competing physical Files with the same known bytes while they were attached to different logical Photos. "
                "This proves a competing identity existed at first observation; it does not reveal how or why the identities were created."
            )
        else:
            classification = INSUFFICIENT_HISTORY
            rationale = (
                "The logical Photos currently share identical known bytes, but immutable revision history is incomplete. "
                "PPA cannot explain whether the identities were already competing at first observation or converged later."
            )

    blockers: list[str] = []
    # Photo-level human notes are identity-specific semantic meaning. A merge
    # must never discard or auto-combine them.
    if any((r["notes"] or "").strip() for r in photo_rows):
        blockers.append("human Photo notes exist for one or more competing logical Photos")
    if len(photo_ids) != 2:
        blockers.append("controlled merge consideration currently requires exactly two logical Photos")
    if any(f.current_sha256 is None for p in photos for f in p.files):
        blockers.append("one or more Files have unknown current SHA-256")
    if any(f.current_sha256 not in (None, sha256) for p in photos for f in p.files):
        blockers.append("one or more logical Photos also own different known current bytes")
    if any(f.library_id != int(library_id) for p in photos for f in p.files):
        blockers.append("one or more competing logical Photos span another Library")

    # Current and historical human organisation both create independent meaning.
    current_org = conn.execute(
        f"""SELECT 1 FROM album_photos WHERE photo_id IN ({ph}) LIMIT 1""", photo_ids
    ).fetchone() or conn.execute(
        f"""SELECT 1 FROM photo_tags WHERE photo_id IN ({ph}) LIMIT 1""", photo_ids
    ).fetchone()
    org_history = conn.execute(
        f"SELECT 1 FROM organization_history WHERE photo_id IN ({ph}) LIMIT 1", photo_ids
    ).fetchone()
    if current_org or org_history:
        blockers.append("Album/Tag curation exists for one or more competing logical Photos")

    lineage_history = conn.execute(
        f"""SELECT 1 FROM photo_lineage_history
              WHERE parent_photo_id IN ({ph}) OR child_photo_id IN ({ph}) LIMIT 1""",
        (*photo_ids, *photo_ids),
    ).fetchone()
    if lineage_history:
        blockers.append("Photo lineage history exists for one or more competing logical Photos")

    identity_history = conn.execute(
        f"""SELECT kind FROM (
                SELECT 'resolution' AS kind FROM identity_resolution_history
                 WHERE source_photo_id IN ({ph}) OR new_photo_id IN ({ph})
                UNION ALL
                SELECT 'merge' AS kind FROM identity_merge_history
                 WHERE survivor_photo_id IN ({ph}) OR retired_photo_id IN ({ph})
             ) LIMIT 1""",
        (*photo_ids, *photo_ids, *photo_ids, *photo_ids),
    ).fetchone()
    if identity_history:
        kind = identity_history["kind"]
        blockers.append(("identity-merge history exists for one or more competing logical Photos")
                        if kind == "merge" else
                        "identity-resolution history exists for one or more competing logical Photos")

    blockers = list(dict.fromkeys(blockers))
    eligible = not blockers
    merge = MergeConsideration(
        eligible,
        "candidate_for_controlled_merge" if eligible else "review_only",
        tuple(blockers),
        ("The two logical Photos are currently byte-equivalent and have no detected independent identity-dependent history. "
         "A future controlled merge workflow may be considered after human review; this investigation does not merge anything.")
        if eligible else
        "Automatic merge consideration is withheld because one or more identity-dependent conditions require human reconciliation.",
    )

    if conn.total_changes != before:
        raise AssertionError("competing identity investigation must remain read-only")
    return CompetingIdentityInvestigation(
        COMPETING_IDENTITY_INVESTIGATION_SCHEMA, int(library_id), sha256,
        classification, rationale, photos, merge,
    )
