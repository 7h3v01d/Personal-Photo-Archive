"""Phase 7.3 — controlled real-collection pilot sessions.

A pilot session is a durable JSON envelope around explicit Phase-7 audit snapshots.
It does not alter chronology, anchors, reconstruction decisions, metadata, or source
photos.  The baseline is captured once, checkpoints are append-only observations,
and closing captures a final snapshot plus a scope-checked comparison.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection
from typing import Callable, Collection

from ppa.pilot_audit import (
    AUDIT_SCHEMA,
    PilotAuditComparison,
    PilotAuditSnapshot,
    build_pilot_audit,
    compare_pilot_audits,
    snapshot_from_dict,
)

SESSION_SCHEMA = "ppa-pilot-session/1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(data: dict) -> str:
    return hashlib.sha256(_canonical(data)).hexdigest()


@dataclass(frozen=True)
class PilotCheckpoint:
    sequence: int
    label: str
    captured_at: str
    snapshot: PilotAuditSnapshot
    snapshot_sha256: str

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "label": self.label,
            "captured_at": self.captured_at,
            "snapshot": self.snapshot.to_dict(),
            "snapshot_sha256": self.snapshot_sha256,
        }


@dataclass(frozen=True)
class PilotSession:
    schema: str
    session_id: str
    created_at: str
    status: str
    library_id: int
    library_root: str
    directory_prefix: str | None
    explicit_file_ids: tuple[str, ...] | None
    baseline: PilotAuditSnapshot
    baseline_sha256: str
    checkpoints: tuple[PilotCheckpoint, ...]
    closed_at: str | None = None
    final: PilotAuditSnapshot | None = None
    final_sha256: str | None = None
    comparison: PilotAuditComparison | None = None

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "status": self.status,
            "scope": {
                "library_id": self.library_id,
                "library_root": self.library_root,
                "directory_prefix": self.directory_prefix,
                "explicit_file_ids": list(self.explicit_file_ids) if self.explicit_file_ids is not None else None,
            },
            "baseline": self.baseline.to_dict(),
            "baseline_sha256": self.baseline_sha256,
            "checkpoints": [c.to_dict() for c in self.checkpoints],
            "closed_at": self.closed_at,
            "final": self.final.to_dict() if self.final else None,
            "final_sha256": self.final_sha256,
            "comparison": self.comparison.to_dict() if self.comparison else None,
            "session_source_writes": 0,
        }

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), indent=2 if pretty else None,
                          sort_keys=True, separators=None if pretty else (",", ":"))


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def start_pilot_session(conn: Connection, *, library_id: int,
                        directory_prefix: str | None = None,
                        file_ids: Collection[str] | None = None,
                        session_id: str | None = None,
                        created_at: str | None = None,
                        progress_cb: Callable[[str], None] | None = None,
                        cancel_cb: Callable[[], bool] | None = None) -> PilotSession:
    snap = build_pilot_audit(conn, library_id=library_id,
                             directory_prefix=directory_prefix, file_ids=file_ids,
                             generated_at=created_at,
                             progress_cb=progress_cb, cancel_cb=cancel_cb)
    sid = session_id or str(uuid.uuid4())
    created = created_at or _utc_now()
    return PilotSession(
        SESSION_SCHEMA, sid, created, "open",
        snap.library_id, snap.library_root, snap.directory_prefix,
        snap.explicit_file_ids, snap, _sha(snap.to_dict()), (),
    )


def save_pilot_session(session: PilotSession, path: Path, *, conn=None, config=None) -> None:
    _validate_session(session)
    from ppa.safe_export import safe_export_text
    text = session.to_json(pretty=True)
    if not text.endswith("\n"):
        text += "\n"
    safe_export_text(Path(path), text, conn=conn, config=config)


def _comparison_from_dict(data: dict | None) -> PilotAuditComparison | None:
    if not data:
        return None
    from ppa.pilot_audit import PilotAuditComparison, AuditDelta
    def d(name: str) -> AuditDelta:
        v = data[name]
        return AuditDelta(int(v["before"]), int(v["after"]), int(v["delta"]))
    return PilotAuditComparison(
        data["schema"], bool(data["same_scope"]), data["before_generated_at"],
        data["after_generated_at"], d("total_files"), d("usable_chronology"),
        d("confirmed_current"), d("proposed_current"), d("stale_decisions"),
        d("unresolved"), d("conflicts"), d("actionable_review"),
        d("anchor_questions"), d("max_anchor_leverage"),
    )


def session_from_dict(data: dict) -> PilotSession:
    if data.get("schema") != SESSION_SCHEMA:
        raise ValueError("unsupported pilot session schema")
    scope = data.get("scope") or {}
    baseline = snapshot_from_dict(data["baseline"])
    checkpoints = []
    for raw in data.get("checkpoints", []):
        snap = snapshot_from_dict(raw["snapshot"])
        checkpoints.append(PilotCheckpoint(int(raw["sequence"]), raw["label"],
                                           raw["captured_at"], snap,
                                           raw["snapshot_sha256"]))
    final = snapshot_from_dict(data["final"]) if data.get("final") else None
    session = PilotSession(
        data["schema"], data["session_id"], data["created_at"], data["status"],
        int(scope["library_id"]), scope["library_root"], scope.get("directory_prefix"),
        tuple(scope["explicit_file_ids"]) if scope.get("explicit_file_ids") is not None else None,
        baseline, data["baseline_sha256"], tuple(checkpoints), data.get("closed_at"),
        final, data.get("final_sha256"), _comparison_from_dict(data.get("comparison")),
    )
    _validate_session(session)
    return session


def load_pilot_session(path: Path) -> PilotSession:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read pilot session: {exc}") from exc
    return session_from_dict(data)


def _scope(session: PilotSession) -> tuple:
    return (session.library_root, session.directory_prefix, session.explicit_file_ids)


def _snapshot_scope(snapshot: PilotAuditSnapshot) -> tuple:
    return (snapshot.library_root, snapshot.directory_prefix, snapshot.explicit_file_ids)


def _validate_session(session: PilotSession) -> None:
    if session.schema != SESSION_SCHEMA:
        raise ValueError("unsupported pilot session schema")
    if session.status not in {"open", "closed"}:
        raise ValueError("invalid pilot session status")
    if session.baseline.schema != AUDIT_SCHEMA:
        raise ValueError("invalid pilot baseline schema")
    if _sha(session.baseline.to_dict()) != session.baseline_sha256:
        raise ValueError("pilot baseline integrity check failed")
    if _scope(session) != _snapshot_scope(session.baseline):
        raise ValueError("pilot session scope does not match baseline")
    expected = 1
    for cp in session.checkpoints:
        if cp.sequence != expected:
            raise ValueError("pilot checkpoint sequence is not contiguous")
        if _sha(cp.snapshot.to_dict()) != cp.snapshot_sha256:
            raise ValueError(f"pilot checkpoint {cp.sequence} integrity check failed")
        if _scope(session) != _snapshot_scope(cp.snapshot):
            raise ValueError("pilot checkpoint scope differs from session")
        expected += 1
    if session.status == "open":
        if session.closed_at or session.final or session.final_sha256 or session.comparison:
            raise ValueError("open pilot session contains close-state data")
    else:
        if not session.closed_at or session.final is None or session.comparison is None:
            raise ValueError("closed pilot session is incomplete")
        if _scope(session) != _snapshot_scope(session.final):
            raise ValueError("pilot final scope differs from session")
        if _sha(session.final.to_dict()) != session.final_sha256:
            raise ValueError("pilot final integrity check failed")


def _capture(conn: Connection, session: PilotSession, *, captured_at: str | None,
             progress_cb=None, cancel_cb=None) -> PilotAuditSnapshot:
    # Resolve by the session's original library id, then enforce stable root/scope.
    snap = build_pilot_audit(conn, library_id=session.library_id,
                             directory_prefix=session.directory_prefix,
                             file_ids=session.explicit_file_ids,
                             generated_at=captured_at,
                             progress_cb=progress_cb, cancel_cb=cancel_cb)
    if _snapshot_scope(snap) != _scope(session):
        raise ValueError("pilot scope no longer resolves to the original library/root")
    return snap


def checkpoint_pilot_session(conn: Connection, session: PilotSession, *,
                             label: str | None = None,
                             captured_at: str | None = None,
                             progress_cb=None, cancel_cb=None) -> PilotSession:
    _validate_session(session)
    if session.status != "open":
        raise ValueError("cannot checkpoint a closed pilot session")
    snap = _capture(conn, session, captured_at=captured_at,
                    progress_cb=progress_cb, cancel_cb=cancel_cb)
    seq = len(session.checkpoints) + 1
    cp = PilotCheckpoint(seq, (label or f"checkpoint-{seq}").strip() or f"checkpoint-{seq}",
                         captured_at or _utc_now(), snap, _sha(snap.to_dict()))
    return PilotSession(
        session.schema, session.session_id, session.created_at, session.status,
        session.library_id, session.library_root, session.directory_prefix,
        session.explicit_file_ids, session.baseline, session.baseline_sha256,
        session.checkpoints + (cp,),
    )


def close_pilot_session(conn: Connection, session: PilotSession, *,
                        closed_at: str | None = None,
                        progress_cb=None, cancel_cb=None) -> PilotSession:
    _validate_session(session)
    if session.status != "open":
        raise ValueError("pilot session is already closed")
    final = _capture(conn, session, captured_at=closed_at,
                     progress_cb=progress_cb, cancel_cb=cancel_cb)
    comparison = compare_pilot_audits(session.baseline, final)
    return PilotSession(
        session.schema, session.session_id, session.created_at, "closed",
        session.library_id, session.library_root, session.directory_prefix,
        session.explicit_file_ids, session.baseline, session.baseline_sha256,
        session.checkpoints, closed_at or _utc_now(), final, _sha(final.to_dict()), comparison,
    )


def concise_text(session: PilotSession) -> str:
    b = session.baseline
    lines = [
        "PPA Pilot Session",
        "=================",
        f"Session: {session.session_id}",
        f"Status: {session.status.upper()}",
        f"Library: {session.library_root}",
        f"Scope: {session.directory_prefix or 'entire library'}",
        f"Baseline files: {b.total_files}",
        f"Baseline usable chronology: {b.usable_chronology.count}",
        f"Baseline unresolved: {b.unresolved.count}",
        f"Checkpoints: {len(session.checkpoints)}",
    ]
    if session.checkpoints:
        latest = session.checkpoints[-1]
        lines += [
            f"Latest checkpoint: {latest.sequence} ({latest.label})",
            f"  usable chronology: {latest.snapshot.usable_chronology.count} "
            f"({latest.snapshot.usable_chronology.count - b.usable_chronology.count:+d})",
            f"  unresolved: {latest.snapshot.unresolved.count} "
            f"({latest.snapshot.unresolved.count - b.unresolved.count:+d})",
        ]
    if session.status == "closed" and session.comparison:
        c = session.comparison
        lines += [
            "", "Final delta", "-----------",
            f"Usable chronology: {c.usable_chronology.before} -> {c.usable_chronology.after} "
            f"({c.usable_chronology.delta:+d})",
            f"Confirmed current: {c.confirmed_current.before} -> {c.confirmed_current.after} "
            f"({c.confirmed_current.delta:+d})",
            f"Unresolved: {c.unresolved.before} -> {c.unresolved.after} ({c.unresolved.delta:+d})",
            f"Stale decisions: {c.stale_decisions.before} -> {c.stale_decisions.after} "
            f"({c.stale_decisions.delta:+d})",
        ]
    lines += ["", "Session source writes: 0"]
    return "\n".join(lines)
