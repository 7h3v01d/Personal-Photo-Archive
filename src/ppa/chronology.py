"""Date Reliability Engine — Phase 6, Slice 2 (cross-photo / sequence evidence).

Slice 1 asks: "what does THIS photograph claim, and can that claim be believed
on its own?" Slice 2 asks: "what do the photographs around it say?" — using
evidence that is independent of the camera clock, chiefly filename order within
a shooting session.

The governing principle (the same one that made Slice 1 strong):

    Filename sequence independently tells us about ORDER, not calendar TRUTH.

So filename order can corroborate "A was taken before B". It CANNOT, on its own,
corroborate "1 January 2001 is the wrong date". A run of sequential frames all
stamped at a reset epoch with a forward-ticking clock is exactly what we'd see
BOTH for a battery-reset camera AND for someone genuinely shooting on 1 January.
Therefore Slice 2 detects that pattern and flags it — but does NOT conclude the
date is wrong. Escalation to LIKELY_WRONG is reserved for a later slice that adds
independent evidence ABOUT THE CALENDAR DATE (a trusted neighbour, a camera
manufacture floor, a user-confirmed event).

Discipline:

  * **Read-only and deterministic.** Layers a combined view over Slice 1's
    intrinsic assessment; never mutates a photo, observation, or that assessment.
  * **Independence must address the claim being escalated.** Order evidence
    escalates nothing here.
  * **Slice 2 only ever adds doubt.** A reset pattern leaves reliability where
    Slice 1 put it (QUESTIONABLE); an order conflict downgrades the *plausible*
    photos it implicates. It never upgrades a doubtful date into a good one.

Sessions are segmented conservatively so unrelated photos aren't compared:
grouping is (library, directory, camera, filename-prefix), then split into
segments by filename-sequence adjacency. We deliberately do NOT segment by the
timestamps themselves — questioning those dates is the whole point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from sqlite3 import Connection

from ppa.dating import (
    DateAssessment,
    DateObservation,
    KNOWN_RESET_EPOCHS,
    Reliability,
    assess,
)

# A run of at least this many adjacent frames at one reset epoch, clock ticking
# forward, is reported as a reset *pattern* (not proof). Configurable.
DEFAULT_MIN_RESET_RUN = 5
# Max filename-sequence gap within one continuous segment (tolerates a few
# deleted frames; a large jump like IMG_0204 -> IMG_6531 breaks the segment).
DEFAULT_MAX_SEQ_GAP = 10
# How far a timestamp may fall out of filename order before it's a conflict.
DEFAULT_REGRESSION_TOLERANCE = timedelta(hours=12)

_TRAILING_DIGITS = re.compile(r"(\d+)(?=\D*$)")


@dataclass
class SequencedPhoto:
    """One photo positioned within a session, with its intrinsic assessment."""
    file_id: str
    filename: str
    seq: int | None                 # numeric filename sequence, if any
    intrinsic: DateAssessment
    camera_id: str | None = None    # confirmed camera identity, if known

    @property
    def candidate(self) -> datetime | None:
        return self.intrinsic.candidate_date


@dataclass
class SequenceFinding:
    kind: str                       # 'reset_pattern' | 'timestamp_order_conflict'
    file_ids: list[str]
    detail: str


@dataclass
class PhotoChronology:
    file_id: str
    intrinsic: Reliability          # Slice 1's rating (unchanged)
    reliability: Reliability        # combined rating after cross-photo evidence
    cross_photo_reasons: list[str] = field(default_factory=list)


def filename_sequence(filename: str) -> tuple[int | None, str]:
    """Split a filename into (numeric sequence, prefix) using the last run of
    digits in the stem (IMG_0201 -> (201, "IMG_"), DSC00042 -> (42, "DSC")).
    """
    stem = filename.rsplit(".", 1)[0]
    m = _TRAILING_DIGITS.search(stem)
    if not m:
        return None, stem
    return int(m.group(1)), stem[:m.start()] + stem[m.end():]


def _is_reset_epoch_day(dt: datetime) -> bool:
    return (dt.year, dt.month, dt.day) in KNOWN_RESET_EPOCHS


def _segment(photos: list[SequencedPhoto], max_seq_gap: int) -> list[list[SequencedPhoto]]:
    """Split filename-ordered photos into continuous segments by sequence
    adjacency. A missing sequence number, a backward jump (counter rollover),
    or a gap larger than ``max_seq_gap`` starts a new segment.
    """
    segments: list[list[SequencedPhoto]] = []
    current: list[SequencedPhoto] = []
    prev: int | None = None
    for p in photos:
        if p.seq is None:
            if current:
                segments.append(current)
            segments.append([p])   # no order evidence -> isolated
            current = []
            prev = None
            continue
        if prev is None or 0 <= (p.seq - prev) <= max_seq_gap:
            current.append(p)
        else:
            if current:
                segments.append(current)
            current = [p]
        prev = p.seq
    if current:
        segments.append(current)
    return segments


def _detect_reset_patterns(seg, chron, findings, min_reset_run):
    """A run of adjacent frames at one reset epoch, clock forward: a *pattern*
    consistent with a running reset clock. Reported, but reliability is left
    where Slice 1 put it — order does not prove the calendar date is wrong.
    """
    i, n = 0, len(seg)
    while i < n:
        p = seg[i]
        if p.candidate is None or not _is_reset_epoch_day(p.candidate):
            i += 1
            continue
        day = p.candidate.date()
        j, last = i + 1, p.candidate
        while j < n and seg[j].candidate is not None \
                and seg[j].candidate.date() == day and seg[j].candidate >= last:
            last = seg[j].candidate
            j += 1
        run = seg[i:j]
        if len(run) >= min_reset_run:
            findings.append(SequenceFinding(
                "reset_pattern", [r.file_id for r in run],
                f"{len(run)} adjacent files ({run[0].filename} … {run[-1].filename}) "
                f"all timestamped {day} with a forward-ticking clock — a pattern "
                "consistent with a running reset clock. Not proof the date is "
                "wrong; needs independent calendar evidence to escalate."))
            for r in run:
                chron[r.file_id].cross_photo_reasons.append(
                    f"In a {len(run)}-frame reset-epoch pattern ({day}); suspicious "
                    "but reliability unchanged pending independent calendar evidence.")
        i = j if j > i else i + 1


def _detect_order_conflicts(seg, chron, findings, regression_tolerance):
    """A later-sequenced frame timestamped earlier than an earlier-sequenced one
    is chronologically inconsistent. We can't tell WHICH date is wrong, so both
    implicated photos inherit doubt — but only when the segment is a confirmed
    single camera (else it may just be interleaved cameras, so: report only).
    """
    camera_known = bool(seg and seg[0].camera_id) and len({p.camera_id for p in seg}) == 1
    dated = [p for p in seg if p.candidate is not None]
    running_max: datetime | None = None
    running_photo: SequencedPhoto | None = None
    for p in dated:
        if running_max is not None and p.candidate < running_max - regression_tolerance:
            findings.append(SequenceFinding(
                "timestamp_order_conflict", [running_photo.file_id, p.file_id],
                f"{p.filename} ({p.candidate.isoformat()}) is earlier than "
                f"earlier-sequenced {running_photo.filename} "
                f"({running_max.isoformat()}); which date is wrong is undetermined."))
            if camera_known:
                for q in (running_photo, p):
                    c = chron[q.file_id]
                    if c.reliability is Reliability.PROBABLY_VALID:
                        c.reliability = Reliability.QUESTIONABLE
                    c.cross_photo_reasons.append(
                        "Timestamp order conflicts with filename order for the same "
                        "camera; which of the conflicting dates is wrong is undetermined.")
        else:
            running_max, running_photo = p.candidate, p


def analyse_sequence(
    photos: list[SequencedPhoto],
    *,
    min_reset_run: int = DEFAULT_MIN_RESET_RUN,
    regression_tolerance: timedelta = DEFAULT_REGRESSION_TOLERANCE,
    max_seq_gap: int = DEFAULT_MAX_SEQ_GAP,
) -> tuple[list[SequenceFinding], dict[str, PhotoChronology]]:
    """Analyse one grouped session (same library/directory/camera/naming scheme),
    ordered by filename sequence. Segments it by sequence adjacency first. Pure
    and deterministic; never upgrades reliability.
    """
    chron = {
        p.file_id: PhotoChronology(p.file_id, p.intrinsic.reliability,
                                   p.intrinsic.reliability)
        for p in photos
    }
    findings: list[SequenceFinding] = []
    for seg in _segment(photos, max_seq_gap):
        _detect_reset_patterns(seg, chron, findings, min_reset_run)
        _detect_order_conflicts(seg, chron, findings, regression_tolerance)
    return findings, chron


# --- catalogue integration ---------------------------------------------------


def _load_sequenced_photos(conn: Connection, **assess_kwargs) -> dict[tuple, list[SequencedPhoto]]:
    """Group present photos into sessions keyed by (library, directory, camera,
    filename prefix), ordered within each by sequence then filename.

    Camera identity comes from files.camera_id (confirmed per current revision),
    NOT from the filename prefix — two cameras that both name files IMG_* must
    not be merged. camera_id IS NULL is kept as its own group value and treated
    conservatively downstream (order-conflicts there are reported, not scored).
    """
    rows = conn.execute(
        "SELECT id, filename, relative_path, library_id, camera_id FROM files "
        "WHERE presence_status = 'present'"
    ).fetchall()

    groups: dict[tuple, list[SequencedPhoto]] = {}
    for r in rows:
        obs_rows = conn.execute(
            "SELECT source, key, value FROM metadata_observations "
            "WHERE file_id = ? AND file_revision_id = "
            "(SELECT current_revision_id FROM files WHERE id = ?)",
            (r["id"], r["id"]),
        ).fetchall()
        obs = [DateObservation(o["source"], o["key"], o["value"]) for o in obs_rows]
        intrinsic = assess(obs, **assess_kwargs)

        rel = (r["relative_path"] or r["filename"]).replace("\\", "/")
        directory = rel.rsplit("/", 1)[0] if "/" in rel else ""
        seq, prefix = filename_sequence(r["filename"])
        key = (r["library_id"], directory, r["camera_id"], prefix)
        groups.setdefault(key, []).append(
            SequencedPhoto(r["id"], r["filename"], seq, intrinsic, r["camera_id"]))

    for photos in groups.values():
        photos.sort(key=lambda p: (p.seq is None, p.seq if p.seq is not None else 0, p.filename))
    return groups


def analyse_library(
    conn: Connection,
    *,
    min_reset_run: int = DEFAULT_MIN_RESET_RUN,
    regression_tolerance: timedelta = DEFAULT_REGRESSION_TOLERANCE,
    max_seq_gap: int = DEFAULT_MAX_SEQ_GAP,
    **assess_kwargs,
) -> tuple[list[SequenceFinding], dict[str, PhotoChronology]]:
    """Cross-photo chronology across the whole catalogue. Read-only."""
    groups = _load_sequenced_photos(conn, **assess_kwargs)
    all_findings: list[SequenceFinding] = []
    combined: dict[str, PhotoChronology] = {}
    for key in sorted(groups, key=lambda k: (k[0] or 0, k[1], str(k[2]), k[3])):
        findings, chron = analyse_sequence(
            groups[key], min_reset_run=min_reset_run,
            regression_tolerance=regression_tolerance, max_seq_gap=max_seq_gap)
        all_findings.extend(findings)
        combined.update(chron)
    return all_findings, combined
