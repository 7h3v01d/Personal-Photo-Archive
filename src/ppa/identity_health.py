"""Phase 10.5 — read-only identity health and resolution triage.

This projection prioritises identity questions without changing Photo/File
identity. Every corrective action remains delegated to the existing controlled
Phase-10 workflows.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from sqlite3 import Connection

IDENTITY_HEALTH_SCHEMA = "ppa-identity-health/1"


@dataclass(frozen=True)
class IdentityHealthItem:
    kind: str
    priority: int
    status: str
    summary: str
    next_action: str
    photo_ids: tuple[str, ...] = ()
    file_ids: tuple[str, ...] = ()
    sha256: str | None = None
    resolution_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class IdentityHealthView:
    schema: str
    library_id: int
    items: tuple[IdentityHealthItem, ...]

    @property
    def competing_identity_count(self) -> int:
        return sum(i.kind == "competing_identity" for i in self.items)

    @property
    def divergence_count(self) -> int:
        return sum(i.kind == "identity_divergence" for i in self.items)

    @property
    def recoverable_split_count(self) -> int:
        return sum(i.kind == "recoverable_split" for i in self.items)

    @property
    def review_only_split_count(self) -> int:
        return sum(i.kind == "review_only_split" for i in self.items)

    @property
    def recovered_split_count(self) -> int:
        return sum(i.kind == "recombined_split" for i in self.items)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "library_id": self.library_id,
            "counts": {
                "competing_identity": self.competing_identity_count,
                "identity_divergence": self.divergence_count,
                "recoverable_split": self.recoverable_split_count,
                "review_only_split": self.review_only_split_count,
                "recombined_split": self.recovered_split_count,
            },
            "items": [
                {
                    **i.__dict__,
                    "photo_ids": list(i.photo_ids),
                    "file_ids": list(i.file_ids),
                }
                for i in self.items
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def _in_clause(values: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    return ",".join("?" for _ in values), values


def build_identity_health(conn: Connection, *, library_id: int) -> IdentityHealthView:
    """Build a deterministic, bounded-query identity triage projection."""
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    before = conn.total_changes

    resolutions = conn.execute(
        "SELECT * FROM identity_resolution_history WHERE library_id=? ORDER BY id", (library_id,)
    ).fetchall()
    touched = tuple(sorted({str(r["source_photo_id"]) for r in resolutions} |
                           {str(r["new_photo_id"]) for r in resolutions}))

    if touched:
        ph, args = _in_clause(touched)
        files = conn.execute(
            f"SELECT id,photo_id,library_id,sha256,presence_status FROM files "
            f"WHERE library_id=? OR photo_id IN ({ph}) ORDER BY photo_id,id",
            (library_id, *args),
        ).fetchall()
        org_history = conn.execute(
            f"SELECT photo_id,created_at FROM organization_history WHERE photo_id IN ({ph})",
            args,
        ).fetchall()
        lineage_history = conn.execute(
            f"SELECT parent_photo_id,child_photo_id,created_at FROM photo_lineage_history "
            f"WHERE parent_photo_id IN ({ph}) OR child_photo_id IN ({ph})",
            (*args, *args),
        ).fetchall()
        all_resolutions = conn.execute(
            f"SELECT resolution_id,source_photo_id,new_photo_id,created_at FROM identity_resolution_history "
            f"WHERE source_photo_id IN ({ph}) OR new_photo_id IN ({ph})",
            (*args, *args),
        ).fetchall()
    else:
        files = conn.execute(
            "SELECT id,photo_id,library_id,sha256,presence_status FROM files WHERE library_id=? ORDER BY photo_id,id",
            (library_id,),
        ).fetchall()
        org_history = (); lineage_history = (); all_resolutions = ()

    recovery_rows = conn.execute(
        "SELECT resolution_id FROM identity_resolution_recovery_history"
    ).fetchall()
    recovered = {r["resolution_id"] for r in recovery_rows}

    library_files = [r for r in files if int(r["library_id"]) == int(library_id)]
    items: list[IdentityHealthItem] = []

    # P0: the same current bytes are represented by multiple logical Photos.
    by_sha: dict[str, list] = {}
    for r in library_files:
        if r["sha256"]:
            by_sha.setdefault(r["sha256"], []).append(r)
    for sha, rows in sorted(by_sha.items()):
        photos = tuple(sorted({r["photo_id"] for r in rows}))
        if len(photos) > 1:
            items.append(IdentityHealthItem(
                "competing_identity", 0, "review_required",
                f"Identical current bytes are assigned to {len(photos)} logical Photos.",
                "Review competing logical Photo identity before creating lineage or splitting further.",
                photos, tuple(sorted(r["id"] for r in rows)), sha,
            ))

    # P1: one logical Photo has multiple known current hashes.
    by_photo: dict[str, list] = {}
    for r in library_files:
        by_photo.setdefault(r["photo_id"], []).append(r)
    for pid, rows in sorted(by_photo.items()):
        hashes = tuple(sorted({r["sha256"] for r in rows if r["sha256"]}))
        if len(hashes) > 1:
            items.append(IdentityHealthItem(
                "identity_divergence", 1, "review_required",
                f"One logical Photo currently contains {len(hashes)} distinct known byte states.",
                "Investigate immutable revision evidence, then use controlled split only if human review supports it.",
                (pid,), tuple(sorted(r["id"] for r in rows)), None,
                reason=f"current hashes: {len(hashes)}",
            ))

    files_by_photo: dict[str, list] = {}
    for r in files:
        files_by_photo.setdefault(r["photo_id"], []).append(r)
    org_by_photo: dict[str, list[str]] = {}
    for r in org_history:
        org_by_photo.setdefault(r["photo_id"], []).append(r["created_at"])
    lineage_by_photo: dict[str, list[str]] = {}
    for r in lineage_history:
        lineage_by_photo.setdefault(r["parent_photo_id"], []).append(r["created_at"])
        lineage_by_photo.setdefault(r["child_photo_id"], []).append(r["created_at"])

    for row in reversed(resolutions):
        rid = row["resolution_id"]
        source = row["source_photo_id"]; new = row["new_photo_id"]; created = row["created_at"]
        if rid in recovered:
            items.append(IdentityHealthItem(
                "recombined_split", 4, "complete",
                "An audited identity split was later safely recombined.",
                "No action required; retain as identity-resolution history.",
                (source, new), resolution_id=rid,
            ))
            continue

        try:
            moved = tuple(json.loads(row["file_ids_json"]))
        except Exception:
            moved = ()
        source_files = files_by_photo.get(source, [])
        new_files = files_by_photo.get(new, [])
        eligible = True; reason = "split remains exactly reversible"
        if not source_files:
            eligible=False; reason="source logical Photo no longer has physical Files"
        elif {r["id"] for r in new_files} != set(moved):
            eligible=False; reason="split-created Photo no longer contains exactly the originally moved File cohort"
        elif any(int(r["library_id"]) != int(library_id) for r in new_files):
            eligible=False; reason="one or more moved Files changed Library ownership"
        elif any(r["sha256"] != row["sha256"] for r in new_files):
            eligible=False; reason="one or more moved Files changed bytes after the split"
        elif any(ts > created for pid in (source,new) for ts in org_by_photo.get(pid,())):
            eligible=False; reason="Album/Tag curation changed one of these logical Photos after the split"
        elif any(ts > created for pid in (source,new) for ts in lineage_by_photo.get(pid,())):
            eligible=False; reason="Photo lineage changed one of these logical Photos after the split"
        elif any(ar["resolution_id"] != rid and ar["created_at"] > created and
                 (ar["source_photo_id"] in (source,new) or ar["new_photo_id"] in (source,new))
                 for ar in all_resolutions):
            eligible=False; reason="a later identity resolution touched one of these logical Photos"
        elif any(r["sha256"] == row["sha256"] and r["photo_id"] not in (source,new) for r in files):
            eligible=False; reason="identical current bytes now belong to another logical Photo"

        if eligible:
            items.append(IdentityHealthItem(
                "recoverable_split", 2, "actionable",
                "An audited split remains provably reversible.",
                "Inspect the resolution topology; recombination is available through the controlled recovery workflow.",
                (source,new), moved, row["sha256"], rid, reason,
            ))
        else:
            items.append(IdentityHealthItem(
                "review_only_split", 3, "review_only",
                "An audited split is no longer safe to recombine automatically.",
                "Inspect the resolution history and current topology; automatic recombination is intentionally disabled.",
                (source,new), moved, row["sha256"], rid, reason,
            ))

    items.sort(key=lambda i: (i.priority, i.kind, i.resolution_id or "", i.photo_ids, i.sha256 or ""))
    if conn.total_changes != before:
        raise AssertionError("identity health projection must be read-only")
    return IdentityHealthView(IDENTITY_HEALTH_SCHEMA, library_id, tuple(items))
