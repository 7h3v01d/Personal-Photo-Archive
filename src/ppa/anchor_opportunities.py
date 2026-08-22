"""Phase 7.2.3 — deterministic anchor opportunity detection.

This layer asks a narrow workflow question: *which single photograph would be
most valuable to date next?*  It does not infer a date and it never creates an
anchor.  It only ranks places where an explicit human date clue could unlock
existing, already-accepted Phase-7 reconstruction logic.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from sqlite3 import Connection
from typing import Collection

from ppa import anchors as anchors_mod
from ppa.pilot import PilotReport, analyse_pilot

QUESTION_SCHEMA = "ppa-anchor-opportunities/1"


@dataclass(frozen=True)
class AnchorQuestion:
    opportunity_id: str
    file_id: str
    filename: str
    recorded: str | None
    priority: str
    affected_file_ids: tuple[str, ...]
    affected_count: int
    group_file_ids: tuple[str, ...]
    group_size: int
    reason: str
    prompt: str


@dataclass(frozen=True)
class AnchorQuestionSet:
    schema: str
    library_id: int
    total_questions: int
    questions: tuple[AnchorQuestion, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), indent=2 if pretty else None,
                          sort_keys=True, separators=None if pretty else (",", ":"))

    @property
    def best(self) -> AnchorQuestion | None:
        return self.questions[0] if self.questions else None


def _opportunity_id(file_ids: Collection[str]) -> str:
    payload = "\n".join(sorted(file_ids)).encode("utf-8")
    return "anchor-" + hashlib.sha256(payload).hexdigest()[:16]


def build_anchor_questions(conn: Connection, *, library_id: int,
                           report: PilotReport | None = None,
                           directory_prefix: str | None = None,
                           file_ids: Collection[str] | None = None,
                           camera_floors=None) -> AnchorQuestionSet:
    """Rank high-value human date questions without modifying the catalogue.

    Only strong-device reset groups are eligible. A group that already contains
    an effective exact human anchor is not a question opportunity: the useful
    next action there is to refresh/review reconstruction, not ask for another
    date. Candidates already covered by any effective human anchor are excluded.
    """
    if report is None:
        report = analyse_pilot(conn, library_id=library_id,
                               directory_prefix=directory_prefix,
                               file_ids=file_ids, camera_floors=camera_floors,
                               generated_at="anchor-opportunities")
    if report.scope.library_id != library_id:
        raise ValueError("pilot report belongs to a different library")

    scope_ids = set().union(*(b.file_ids for b in report.review_priority.values()))
    if not scope_ids:
        return AnchorQuestionSet(QUESTION_SCHEMA, library_id, 0, ())

    marks = ",".join("?" for _ in scope_ids)
    rows = conn.execute(
        f"SELECT id, filename, relative_path FROM files "
        f"WHERE library_id=? AND id IN ({marks})",
        (library_id, *sorted(scope_ids)),
    ).fetchall()
    row_by = {r["id"]: r for r in rows}
    all_anchors = anchors_mod.list_anchors(conn)

    def effective_anchor(fid: str):
        r = row_by[fid]
        rel = (r["relative_path"] or r["filename"]).replace("\\", "/")
        directory = rel.rsplit("/", 1)[0] if "/" in rel else ""
        return anchors_mod.resolve_for(all_anchors, file_id=fid,
                                       directory=directory, library_id=library_id)

    unresolved = set().union(*(b.file_ids for b in report.unresolved.values()))
    questions: list[AnchorQuestion] = []
    for group in report.reset_groups:
        if group.identity_strength != "DEVICE_STRONG" or group.file_count < 2:
            continue
        members = tuple(fid for fid in group.file_ids if fid in scope_ids and fid in row_by)
        if len(members) < 2:
            continue
        anchors_by = {fid: effective_anchor(fid) for fid in members}
        # An existing exact human anchor already gives Phase 7 the strongest
        # calendar witness this workflow is designed to solicit.
        if any(a is not None and a.kind == "exact" for a in anchors_by.values()):
            continue

        candidates = [fid for fid in members if fid in unresolved and anchors_by[fid] is None]
        if not candidates:
            continue

        # Deterministic neutral choice: the sequence midpoint among unresolved,
        # unanchored members.  This is NOT a dating inference; it merely avoids
        # systematically asking about the first/last frame of every run.
        ordered = sorted(candidates, key=lambda fid: (row_by[fid]["filename"].casefold(), fid))
        candidate = ordered[len(ordered) // 2]
        affected = tuple(sorted(fid for fid in members if fid != candidate and fid in unresolved))
        if not affected:
            continue

        recorded = group.recorded_start
        # Prefer the candidate's actual candidate timestamp when available from
        # the current observations; this is display context only.
        obs = conn.execute(
            "SELECT value FROM metadata_observations WHERE file_id=? "
            "AND source='exif' AND key='DateTimeOriginal' AND file_revision_id="
            "(SELECT current_revision_id FROM files WHERE id=?) ORDER BY id DESC LIMIT 1",
            (candidate, candidate),
        ).fetchone()
        if obs is not None and obs["value"]:
            recorded = obs["value"]

        n = len(affected)
        priority = "A" if n >= 10 else "B"
        reason = (f"An exact human date for this frame could constrain up to {n} other "
                  f"unresolved photo(s) in the same strong-device reset run.")
        questions.append(AnchorQuestion(
            _opportunity_id(members), candidate, row_by[candidate]["filename"], recorded,
            priority, affected, n, tuple(sorted(members)), len(members), reason,
            "Do you know when this photograph was taken?",
        ))

    questions.sort(key=lambda q: (-q.affected_count, q.filename.casefold(), q.file_id))
    return AnchorQuestionSet(QUESTION_SCHEMA, library_id, len(questions), tuple(questions))


def concise_text(question_set: AnchorQuestionSet) -> str:
    lines = ["PPA Anchor Opportunities", "========================", "",
             f"Library: {question_set.library_id}",
             f"Questions: {question_set.total_questions}", ""]
    if not question_set.questions:
        lines.append("No high-value human date question is currently supported.")
        return "\n".join(lines)
    for n, q in enumerate(question_set.questions, 1):
        lines.append(f"{n:3}. [{q.priority}] {q.filename} — may help {q.affected_count} other photo(s)")
        lines.append(f"     {q.reason}")
    return "\n".join(lines)
