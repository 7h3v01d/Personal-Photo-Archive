"""Phase 13.0 — dry-run archive recovery planning and donor qualification.

Phase 12 established what PPA may assert about expected, catalogue-current and
physically-current bytes.  Phase 13 begins recovery, but this first slice does
*not* cross the source-file write boundary.  It answers a narrower question:

    If the human explicitly retained an expected FileRevision and said recovery
    is needed, is there a donor File whose current physical bytes can be proved
    to reproduce that exact immutable revision, and what would a later recovery
    have to do?

Donor qualification is fail-closed.  A donor must have verified-current
catalogue identity, be free of unresolved origin ambiguity, and then be freshly
re-attested from disk.  Filesystem object/device observations are exposed as
current topology evidence only; they are never promoted into an "independent
backup" claim.

Planning may append an immutable catalogue proposal record, but no function in
this module writes, replaces, renames, moves, deletes or repairs a source photo.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from sqlite3 import Connection
import uuid

from ppa.current_identity import verified_current_sha256_sql
from ppa.physical_observation import (
    PhysicalObservationError,
    StableFileObservation,
    observe_stable_image,
)

RECOVERY_PLANNING_VIEW_SCHEMA = "ppa-recovery-planning/1"
RECOVERY_PLAN_SCHEMA = "ppa-recovery-plan/1"

ACTION_RESTORE_MISSING = "restore_missing_destination_from_verified_donor"
ACTION_REPLACE_MISMATCH = "preserve_suspect_then_restore_expected_from_verified_donor"
ACTION_REPLACE_UNREADABLE = "preserve_unreadable_then_restore_expected_from_verified_donor"


class RecoveryPlanningError(ValueError):
    """Recovery cannot be planned from the currently proven evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class RecoveryDonorCandidate:
    file_id: str
    photo_id: str
    library_id: int
    path: str
    current_revision_id: str | None
    expected_sha256: str | None
    verified_current_sha256: str | None
    presence_status: str
    health_status: str
    origin_ambiguous: bool
    physical_state: str | None
    physical_sha256: str | None
    fs_device_id: str | None
    fs_object_id: str | None
    topology_class: str
    same_logical_photo: bool
    same_library: bool
    qualified: bool
    rejection_reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RecoveryPlanningView:
    schema: str
    read_only_sources: bool
    file_id: str
    photo_id: str
    library_id: int
    path: str
    expected_revision_id: str
    expected_sha256: str
    recovery_intent_resolution_id: str
    recovery_intent_decision_id: str
    recovery_intent_at: str
    target_state: str
    target_observed_sha256: str | None
    target_fs_device_id: str | None
    target_fs_object_id: str | None
    candidates: tuple[RecoveryDonorCandidate, ...]
    preferred_donor_file_id: str | None
    notes: tuple[str, ...]

    @property
    def qualified_candidates(self) -> tuple[RecoveryDonorCandidate, ...]:
        return tuple(c for c in self.candidates if c.qualified)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["qualified_donor_file_ids"] = [c.file_id for c in self.qualified_candidates]
        data["qualified_donor_count"] = len(self.qualified_candidates)
        return data

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


@dataclass(frozen=True)
class RecoveryPlan:
    schema: str
    proposal_id: str
    dry_run_only: bool
    execution_authorized: bool
    file_id: str
    photo_id: str
    library_id: int
    target_path: str
    expected_revision_id: str
    expected_sha256: str
    recovery_intent_resolution_id: str
    recovery_intent_decision_id: str
    donor_file_id: str
    donor_photo_id: str
    donor_library_id: int
    donor_path: str
    donor_revision_id: str
    donor_sha256: str
    target_state: str
    target_observed_sha256: str | None
    target_size_bytes: int | None
    target_mtime_ns: int | None
    target_fs_device_id: str | None
    target_fs_object_id: str | None
    donor_size_bytes: int
    donor_mtime_ns: int
    donor_fs_device_id: str | None
    donor_fs_object_id: str | None
    topology_class: str
    same_logical_photo: bool
    same_library: bool
    independent_backup_claim: bool
    proposed_action: tuple[str, ...]
    evidence_fingerprint: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


@dataclass(frozen=True)
class RecordedRecoveryProposal:
    proposal_id: str
    target_file_id: str
    donor_file_id: str
    evidence_fingerprint: str
    proposed_at: str
    proposal_state: str = "dry_run_not_executed"


def _latest_current_resolution(conn: Connection, file_id: str, revision_id: str):
    return conn.execute(
        """
        SELECT resolution_id,decision_id,action,expected_revision_id,expected_sha256,resolved_at
          FROM integrity_mismatch_resolutions
         WHERE file_id=? AND expected_revision_id=?
         ORDER BY id DESC
         LIMIT 1
        """,
        (file_id, revision_id),
    ).fetchone()


def _target_row(conn: Connection, file_id: str):
    return conn.execute(
        """
        SELECT f.id,f.photo_id,f.library_id,f.path,f.presence_status,f.health_status,
               f.current_revision_id,r.sha256 AS expected_sha256,r.superseded_at
          FROM files f
          LEFT JOIN file_revisions r ON r.id=f.current_revision_id AND r.file_id=f.id
         WHERE f.id=?
        """,
        (file_id,),
    ).fetchone()


def _required_recovery_intent(conn: Connection, file_id: str):
    row = _target_row(conn, file_id)
    if row is None:
        raise RecoveryPlanningError("unknown File")
    if not row["current_revision_id"] or not row["expected_sha256"]:
        raise RecoveryPlanningError("File has no immutable expected FileRevision to recover")
    if row["superseded_at"] is not None:
        raise RecoveryPlanningError("current expected FileRevision is superseded; refresh the integrity investigation")
    if row["health_status"] != "hash_mismatch":
        raise RecoveryPlanningError("File is not currently in verified hash-mismatch health")
    latest = _latest_current_resolution(conn, file_id, row["current_revision_id"])
    if latest is None or latest["action"] != "retain_expected_recovery_needed":
        raise RecoveryPlanningError(
            "latest human disposition for this mismatch is not 'retain expected / recovery needed'"
        )
    if latest["expected_sha256"] != row["expected_sha256"]:
        raise RecoveryPlanningError("recovery intent no longer matches the current expected revision")
    return row, latest


def _observe_target(path: str, expected_sha256: str) -> StableFileObservation:
    try:
        observation = observe_stable_image(Path(path), expected_sha256=expected_sha256)
    except PhysicalObservationError as exc:
        raise RecoveryPlanningError(
            "target File changed while recovery evidence was being observed; run Verify / investigate again"
        ) from exc
    if observation.state == "matches_expected":
        raise RecoveryPlanningError(
            "target bytes already reproduce the expected FileRevision; run Verify to reconcile health instead of planning recovery"
        )
    return observation


def _topology(target: StableFileObservation, donor: StableFileObservation) -> str:
    if target.fs_device_id is None or target.fs_object_id is None:
        return "target_storage_identity_unavailable"
    if donor.fs_device_id is None or donor.fs_object_id is None:
        return "donor_storage_identity_unavailable"
    if (target.fs_device_id, target.fs_object_id) == (donor.fs_device_id, donor.fs_object_id):
        return "same_filesystem_object"
    if target.fs_device_id == donor.fs_device_id:
        return "distinct_filesystem_objects_same_device_id"
    return "distinct_filesystem_device_ids"


def _candidate_rows(conn: Connection, *, target_file_id: str, expected_sha256: str):
    current_sha = verified_current_sha256_sql("f", "r")
    return conn.execute(
        f"""
        SELECT f.id,f.photo_id,f.library_id,f.path,f.presence_status,f.health_status,f.current_revision_id,
               r.sha256 AS expected_sha256,{current_sha} AS verified_current_sha256,
               EXISTS(SELECT 1 FROM file_origin_ambiguities a WHERE a.observed_file_id=f.id) AS origin_ambiguous
          FROM files f
          LEFT JOIN file_revisions r ON r.id=f.current_revision_id AND r.file_id=f.id
         WHERE f.id<>? AND r.sha256=?
         ORDER BY CASE WHEN f.library_id=(SELECT library_id FROM files WHERE id=?) THEN 0 ELSE 1 END,
                  CASE WHEN f.photo_id=(SELECT photo_id FROM files WHERE id=?) THEN 0 ELSE 1 END,
                  f.path,f.id
        """,
        (target_file_id, expected_sha256, target_file_id, target_file_id),
    ).fetchall()


def _qualified_candidates(
    conn: Connection,
    *,
    target_row,
    target_observation: StableFileObservation,
) -> tuple[RecoveryDonorCandidate, ...]:
    expected_sha = str(target_row["expected_sha256"])
    out: list[RecoveryDonorCandidate] = []
    for row in _candidate_rows(
        conn,
        target_file_id=str(target_row["id"]),
        expected_sha256=expected_sha,
    ):
        reasons: list[str] = []
        physical: StableFileObservation | None = None
        verified = row["verified_current_sha256"]
        if row["presence_status"] != "present":
            reasons.append("donor is not currently present")
        if row["health_status"] != "ok":
            reasons.append(f"donor health is {row['health_status']}")
        if verified != expected_sha:
            reasons.append("donor lacks verified-current identity for the expected SHA-256")
        if bool(row["origin_ambiguous"]):
            reasons.append("donor has recorded ambiguous physical-file origin")

        # Only a catalogue-eligible donor earns a source read.  Physical reality
        # may still invalidate it, including an unseen external edit.
        if not reasons:
            try:
                physical = observe_stable_image(Path(row["path"]), expected_sha256=expected_sha)
            except PhysicalObservationError:
                reasons.append("donor changed while it was being physically re-attested")
            else:
                if physical.state != "matches_expected" or physical.sha256 != expected_sha:
                    reasons.append("donor physical bytes do not currently reproduce the expected SHA-256")

        topology = _topology(target_observation, physical) if physical is not None else "not_qualified"
        if topology == "same_filesystem_object":
            reasons.append("donor and target resolve to the same filesystem object")

        out.append(RecoveryDonorCandidate(
            file_id=str(row["id"]),
            photo_id=str(row["photo_id"]),
            library_id=int(row["library_id"]),
            path=str(row["path"]),
            current_revision_id=None if row["current_revision_id"] is None else str(row["current_revision_id"]),
            expected_sha256=None if row["expected_sha256"] is None else str(row["expected_sha256"]),
            verified_current_sha256=None if verified is None else str(verified),
            presence_status=str(row["presence_status"]),
            health_status=str(row["health_status"]),
            origin_ambiguous=bool(row["origin_ambiguous"]),
            physical_state=None if physical is None else physical.state,
            physical_sha256=None if physical is None else physical.sha256,
            fs_device_id=None if physical is None else physical.fs_device_id,
            fs_object_id=None if physical is None else physical.fs_object_id,
            topology_class=topology,
            same_logical_photo=str(row["photo_id"]) == str(target_row["photo_id"]),
            same_library=int(row["library_id"]) == int(target_row["library_id"]),
            qualified=not reasons,
            rejection_reasons=tuple(reasons),
        ))
    return tuple(out)


def _candidate_rank(candidate: RecoveryDonorCandidate) -> tuple[int, int, int, str, str]:
    topology_rank = {
        "distinct_filesystem_device_ids": 0,
        "distinct_filesystem_objects_same_device_id": 1,
        "target_storage_identity_unavailable": 2,
        "donor_storage_identity_unavailable": 3,
    }.get(candidate.topology_class, 9)
    # Prefer a donor already belonging to the same logical Photo when physical
    # topology is otherwise equally strong; content proof still remains primary.
    return (
        topology_rank,
        0 if candidate.same_logical_photo else 1,
        0 if candidate.same_library else 1,
        candidate.path.casefold(),
        candidate.file_id,
    )


def build_recovery_planning_view(conn: Connection, *, file_id: str) -> RecoveryPlanningView:
    row, intent = _required_recovery_intent(conn, file_id)
    target = _observe_target(str(row["path"]), str(row["expected_sha256"]))
    candidates = _qualified_candidates(conn, target_row=row, target_observation=target)
    qualified = sorted((c for c in candidates if c.qualified), key=_candidate_rank)
    notes = [
        "No source photo is modified by Phase 13.0 recovery planning.",
        "Filesystem device/object identity is topology evidence, not proof of an independent physical backup or failure domain.",
    ]
    if target.state == "missing":
        notes.append("The recovery destination is currently missing; destination-object topology cannot be compared until a later execution boundary.")
    if not qualified:
        notes.append("No donor currently satisfies both catalogue authority and fresh physical-byte re-attestation.")

    return RecoveryPlanningView(
        schema=RECOVERY_PLANNING_VIEW_SCHEMA,
        read_only_sources=True,
        file_id=str(row["id"]),
        photo_id=str(row["photo_id"]),
        library_id=int(row["library_id"]),
        path=str(row["path"]),
        expected_revision_id=str(row["current_revision_id"]),
        expected_sha256=str(row["expected_sha256"]),
        recovery_intent_resolution_id=str(intent["resolution_id"]),
        recovery_intent_decision_id=str(intent["decision_id"]),
        recovery_intent_at=str(intent["resolved_at"]),
        target_state=target.state,
        target_observed_sha256=target.sha256,
        target_fs_device_id=target.fs_device_id,
        target_fs_object_id=target.fs_object_id,
        candidates=candidates,
        preferred_donor_file_id=None if not qualified else qualified[0].file_id,
        notes=tuple(notes),
    )


def _action_for_target(target_state: str) -> tuple[str, tuple[str, ...]]:
    common_tail = (
        "materialize donor bytes to a new staging file on the destination filesystem",
        "fsync/close staging output and prove staged SHA-256 equals the immutable expected revision",
        "future execution phase may atomically place verified staged bytes at the recorded destination path",
        "run Verify after any future recovery write before health may return to ok",
    )
    if target_state == "missing":
        return ACTION_RESTORE_MISSING, common_tail
    if target_state == "still_mismatched":
        return ACTION_REPLACE_MISMATCH, (
            "preserve the currently mismatching destination bytes as recovery evidence before any replacement",
        ) + common_tail
    if target_state == "unreadable":
        return ACTION_REPLACE_UNREADABLE, (
            "preserve the currently unreadable destination bytes as recovery evidence before any replacement",
        ) + common_tail
    raise RecoveryPlanningError(f"target state {target_state!r} is not recoverable by this planner")


def _plan_fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_recovery_plan(
    conn: Connection,
    *,
    file_id: str,
    donor_file_id: str | None = None,
    proposal_id: str | None = None,
) -> RecoveryPlan:
    view = build_recovery_planning_view(conn, file_id=file_id)
    qualified = {c.file_id: c for c in view.qualified_candidates}
    selected_id = donor_file_id or view.preferred_donor_file_id
    if selected_id is None:
        raise RecoveryPlanningError("no qualified recovery donor is currently available")
    donor = qualified.get(selected_id)
    if donor is None:
        matching = next((c for c in view.candidates if c.file_id == selected_id), None)
        if matching is None:
            raise RecoveryPlanningError("requested donor is not a same-revision candidate for this target")
        reason = "; ".join(matching.rejection_reasons) or "candidate is not qualified"
        raise RecoveryPlanningError(f"requested donor is not qualified: {reason}")
    if donor.current_revision_id is None or donor.physical_sha256 != view.expected_sha256:
        raise RecoveryPlanningError("qualified donor evidence is incomplete")

    # Re-observe target and donor so the plan fingerprint binds one fresh pair of
    # physical observations rather than relying on objects collected earlier in
    # the candidate-list pass.
    target = _observe_target(view.path, view.expected_sha256)
    try:
        donor_obs = observe_stable_image(Path(donor.path), expected_sha256=view.expected_sha256)
    except PhysicalObservationError as exc:
        raise RecoveryPlanningError("donor changed while the recovery plan was being built") from exc
    if donor_obs.state != "matches_expected" or donor_obs.sha256 != view.expected_sha256:
        raise RecoveryPlanningError("donor bytes changed after qualification; rebuild recovery planning view")
    topology = _topology(target, donor_obs)
    if topology == "same_filesystem_object":
        raise RecoveryPlanningError("donor and target are the same filesystem object; no independent recovery source exists")

    action_name, steps = _action_for_target(target.state)
    action_steps = (f"planned action: {action_name}",) + steps
    evidence = {
        "target": {
            "file_id": view.file_id,
            "photo_id": view.photo_id,
            "library_id": view.library_id,
            "path": view.path,
            "expected_revision_id": view.expected_revision_id,
            "expected_sha256": view.expected_sha256,
            "recovery_intent_resolution_id": view.recovery_intent_resolution_id,
            "recovery_intent_decision_id": view.recovery_intent_decision_id,
            "state": target.state,
            "sha256": target.sha256,
            "size_bytes": target.size_bytes,
            "mtime_ns": target.mtime_ns,
            "fs_device_id": target.fs_device_id,
            "fs_object_id": target.fs_object_id,
        },
        "donor": {
            "file_id": donor.file_id,
            "photo_id": donor.photo_id,
            "library_id": donor.library_id,
            "path": donor.path,
            "revision_id": donor.current_revision_id,
            "sha256": donor_obs.sha256,
            "size_bytes": donor_obs.size_bytes,
            "mtime_ns": donor_obs.mtime_ns,
            "fs_device_id": donor_obs.fs_device_id,
            "fs_object_id": donor_obs.fs_object_id,
        },
        "topology_class": topology,
        "same_logical_photo": donor.same_logical_photo,
        "same_library": donor.same_library,
        "independent_backup_claim": False,
        "proposed_action": action_steps,
        "execution_authorized": False,
    }
    fingerprint = _plan_fingerprint(evidence)
    return RecoveryPlan(
        schema=RECOVERY_PLAN_SCHEMA,
        proposal_id=proposal_id or str(uuid.uuid4()),
        dry_run_only=True,
        execution_authorized=False,
        file_id=view.file_id,
        photo_id=view.photo_id,
        library_id=view.library_id,
        target_path=view.path,
        expected_revision_id=view.expected_revision_id,
        expected_sha256=view.expected_sha256,
        recovery_intent_resolution_id=view.recovery_intent_resolution_id,
        recovery_intent_decision_id=view.recovery_intent_decision_id,
        donor_file_id=donor.file_id,
        donor_photo_id=donor.photo_id,
        donor_library_id=donor.library_id,
        donor_path=donor.path,
        donor_revision_id=donor.current_revision_id,
        donor_sha256=str(donor_obs.sha256),
        target_state=target.state,
        target_observed_sha256=target.sha256,
        target_size_bytes=target.size_bytes,
        target_mtime_ns=target.mtime_ns,
        target_fs_device_id=target.fs_device_id,
        target_fs_object_id=target.fs_object_id,
        donor_size_bytes=int(donor_obs.size_bytes or 0),
        donor_mtime_ns=int(donor_obs.mtime_ns or 0),
        donor_fs_device_id=donor_obs.fs_device_id,
        donor_fs_object_id=donor_obs.fs_object_id,
        topology_class=topology,
        same_logical_photo=donor.same_logical_photo,
        same_library=donor.same_library,
        independent_backup_claim=False,
        proposed_action=action_steps,
        evidence_fingerprint=fingerprint,
    )


def record_recovery_plan_proposal(
    conn: Connection,
    plan: RecoveryPlan,
    *,
    note: str | None = None,
) -> RecordedRecoveryProposal:
    """Append one dry-run proposal after revalidating its complete evidence.

    This records that the plan was proposed, not that execution was authorised or
    performed.  Source photographs remain read-only.
    """
    if plan.schema != RECOVERY_PLAN_SCHEMA or not plan.dry_run_only or plan.execution_authorized:
        raise RecoveryPlanningError("invalid Phase-13.0 recovery plan")
    note = None if note is None or not str(note).strip() else str(note).strip()
    if note is not None and len(note) > 4000:
        raise RecoveryPlanningError("recovery-plan note is too long")

    try:
        conn.execute("BEGIN IMMEDIATE")
        rebuilt = build_recovery_plan(
            conn,
            file_id=plan.file_id,
            donor_file_id=plan.donor_file_id,
            proposal_id=plan.proposal_id,
        )
        if rebuilt.evidence_fingerprint != plan.evidence_fingerprint:
            raise RecoveryPlanningError(
                "recovery plan is stale: target or donor evidence changed; rebuild the dry-run plan"
            )
        if rebuilt.recovery_intent_resolution_id != plan.recovery_intent_resolution_id:
            raise RecoveryPlanningError("recovery intent changed; rebuild the dry-run plan")
        proposed_at = _now()
        conn.execute(
            """
            INSERT INTO archive_recovery_plan_proposals(
                proposal_id,recovery_intent_resolution_id,target_file_id,target_photo_id,library_id,
                expected_revision_id,expected_sha256,donor_file_id,donor_photo_id,donor_library_id,donor_revision_id,donor_sha256,
                target_path,target_state,target_observed_sha256,target_size_bytes,target_mtime_ns,
                target_fs_device_id,target_fs_object_id,donor_path,donor_size_bytes,donor_mtime_ns,
                donor_fs_device_id,donor_fs_object_id,topology_class,same_logical_photo,same_library,
                independent_backup_claim,proposed_action_json,evidence_fingerprint,note,proposed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                plan.proposal_id,plan.recovery_intent_resolution_id,plan.file_id,plan.photo_id,plan.library_id,
                plan.expected_revision_id,plan.expected_sha256,plan.donor_file_id,plan.donor_photo_id,
                plan.donor_library_id,plan.donor_revision_id,plan.donor_sha256,plan.target_path,plan.target_state,
                plan.target_observed_sha256,plan.target_size_bytes,plan.target_mtime_ns,
                plan.target_fs_device_id,plan.target_fs_object_id,plan.donor_path,plan.donor_size_bytes,
                plan.donor_mtime_ns,plan.donor_fs_device_id,plan.donor_fs_object_id,plan.topology_class,
                1 if plan.same_logical_photo else 0,1 if plan.same_library else 0,0,
                json.dumps(plan.proposed_action, ensure_ascii=False),
                plan.evidence_fingerprint,note,proposed_at,
            ),
        )
        conn.execute(
            "INSERT INTO integrity_events(file_id,event_type,detail) VALUES (?,?,?)",
            (
                plan.file_id,
                "archive_recovery_plan_proposed",
                f"Dry-run recovery proposal {plan.proposal_id} recorded using donor {plan.donor_file_id}; "
                f"expected_sha256={plan.expected_sha256}. Recovery was proposed but not executed; no recovery write was authorised.",
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return RecordedRecoveryProposal(
        proposal_id=plan.proposal_id,
        target_file_id=plan.file_id,
        donor_file_id=plan.donor_file_id,
        evidence_fingerprint=plan.evidence_fingerprint,
        proposed_at=proposed_at,
    )


def list_recovery_plan_proposals(conn: Connection, *, target_file_id: str | None = None):
    if target_file_id is None:
        return conn.execute(
            "SELECT * FROM archive_recovery_plan_proposals ORDER BY proposed_at,id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM archive_recovery_plan_proposals WHERE target_file_id=? ORDER BY proposed_at,id",
        (target_file_id,),
    ).fetchall()


def concise_planning_text(view: RecoveryPlanningView) -> str:
    lines = [
        "Phase 13.0 — Recovery Planning & Donor Qualification",
        f"Target: {view.file_id}  {view.path}",
        f"Expected SHA-256: {view.expected_sha256}",
        f"Target state: {view.target_state}",
        f"Recovery intent: {view.recovery_intent_resolution_id}",
        f"Qualified donors: {len(view.qualified_candidates)} / {len(view.candidates)}",
    ]
    for candidate in view.candidates:
        state = "QUALIFIED" if candidate.qualified else "REJECTED"
        detail = candidate.topology_class
        if candidate.rejection_reasons:
            detail += " — " + "; ".join(candidate.rejection_reasons)
        lines.append(f"  {state}: {candidate.file_id}  L{candidate.library_id}  {candidate.path}  [{detail}]")
    if view.preferred_donor_file_id:
        lines.append(f"Preferred donor: {view.preferred_donor_file_id}")
    lines.append("No independent-backup claim is made from filesystem topology.")
    return "\n".join(lines)


def concise_plan_text(plan: RecoveryPlan) -> str:
    lines = [
        "Phase 13.0 — DRY-RUN recovery plan",
        f"Proposal: {plan.proposal_id}",
        f"Target: {plan.file_id}  {plan.target_path}",
        f"Donor:  {plan.donor_file_id}  L{plan.donor_library_id}  {plan.donor_path}",
        f"Expected SHA-256: {plan.expected_sha256}",
        f"Topology: {plan.topology_class}",
        "Independent backup proven: NO",
        "Execution authorised: NO",
        f"Evidence fingerprint: {plan.evidence_fingerprint}",
        "Proposed future action:",
    ]
    lines.extend(f"  {index}. {step}" for index, step in enumerate(plan.proposed_action, start=1))
    return "\n".join(lines)
