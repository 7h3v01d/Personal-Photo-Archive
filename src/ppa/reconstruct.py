"""Historical Date Reconstruction — Phase 7.0.1 (pure engine).

Phase 6 answers "is this date credible?". Phase 7 answers "if 2001 is wrong,
WHEN was this actually taken?" — producing an *interpreted* capture date or range
with a confidence and an evidence chain. Read-only and deterministic; it never
mutates observations or the recorded date. Persistence and wiring are 7.1.

Governing rule (the one that has protected this project since Phase 1):

    A reconstruction may never be more precise or more certain than the evidence
    that supports it.

Consequences enforced here:

  * **Offset propagation is anchored only by an EXACT human/local calendar date.**
    EXIF ``GPSDateStamp`` is UTC-derived while ``DateTimeOriginal`` is normally
    local and timezone-less, so a GPS date can sit a day either side of the true
    local date. Using it as an exact reset-clock offset would push a whole run a
    day off at STRONG confidence. GPS therefore reconstructs only its own frame,
    as a ±1-day RANGE, and never anchors an offset.
  * **Filename-order bracketing requires a strong single-device ordering.** In a
    model-only group (possibly two bodies), frame N's number does not establish
    chronological position, so bracketing there could fabricate a range.
  * **Offset only revises QUESTIONABLE/LIKELY_WRONG frames** — never a clean claim
    that independent evidence hasn't actually contradicted.
  * The offset anchor must belong to the run itself (no external anchors yet).

See docs/PHASE7_DESIGN.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from ppa.dating import Reliability

# Bumped when the reconstruction rules change, so a stored proposal records which
# engine produced it (reproducibility / staleness reasoning).
ENGINE_VERSION = "7.0.1"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"   # human anchor or direct independent evidence
    STRONG = "strong"         # offset propagation in a confirmed single-device run
    RANGE = "range"           # bracketed/anchored/GPS-derived interval, not a point
    PROPOSED = "proposed"     # weaker inference; shown, never treated as fact


class KnownTrueKind(str, Enum):
    HUMAN_EXACT = "human_exact"   # exact user anchor: a local calendar date
    GPS_DATE = "gps_date"         # EXIF GPSDateStamp: UTC-derived, date-only


# Days either side of a GPS UTC date within which the true LOCAL date must fall.
_GPS_LOCAL_SLACK = timedelta(days=1)


@dataclass
class ReconstructionInput:
    file_id: str
    recorded: datetime | None            # recorded capture instant (the candidate)
    reliability: Reliability             # Phase 6 final rating
    reset_group: str | None = None
    reset_group_strong: bool = False     # confirmed single device (strong ordering id)
    seq: int | None = None               # filename sequence, for bracketing order
    known_true: date | None = None       # independent true date for THIS frame
    known_true_kind: KnownTrueKind | None = None
    anchor_range: tuple[date, date] | None = None   # (start, end) inclusive


@dataclass
class Reconstruction:
    file_id: str
    start: date
    end: date | None                     # None => point date; else inclusive range
    confidence: Confidence
    method: str                          # 'direct'|'direct_gps'|'offset'|'anchor_range'|'bracket'
    evidence: str = ""


_REVISABLE = (Reliability.QUESTIONABLE, Reliability.LIKELY_WRONG)


def _validate(inputs: list[ReconstructionInput]) -> None:
    seen: set[str] = set()
    for i in inputs:
        if i.file_id in seen:
            raise ValueError(f"duplicate file_id in reconstruction input: {i.file_id}")
        seen.add(i.file_id)
        if i.known_true is not None and not isinstance(i.known_true_kind, KnownTrueKind):
            raise ValueError(
                f"{i.file_id}: known_true requires a KnownTrueKind, got "
                f"{i.known_true_kind!r}")
        if i.anchor_range is not None:
            s, e = i.anchor_range
            if e < s:
                raise ValueError(f"{i.file_id}: anchor_range end {e} precedes start {s}")


def _recorded_date(i: ReconstructionInput) -> date | None:
    return i.recorded.date() if i.recorded is not None else None


def reconstruct(inputs: list[ReconstructionInput]) -> dict[str, Reconstruction]:
    """Reconstruct interpreted capture dates from Phase-6 output and independent
    evidence. Pure and deterministic; validates its own trust boundary."""
    _validate(inputs)
    out: dict[str, Reconstruction] = {}

    # 1. Direct, per frame.
    for i in inputs:
        if i.known_true is None:
            continue
        if i.known_true_kind is KnownTrueKind.HUMAN_EXACT:
            out[i.file_id] = Reconstruction(
                i.file_id, i.known_true, None, Confidence.CONFIRMED, "direct",
                f"Exact human-confirmed date {i.known_true.isoformat()}.")
        else:  # GPS_DATE: UTC-derived -> true LOCAL date within ±1 day.
            lo = i.known_true - _GPS_LOCAL_SLACK
            hi = i.known_true + _GPS_LOCAL_SLACK
            out[i.file_id] = Reconstruction(
                i.file_id, lo, hi, Confidence.RANGE, "direct_gps",
                f"GPS (UTC) date {i.known_true.isoformat()}; true local date within "
                f"{lo.isoformat()}…{hi.isoformat()}.")

    # 2. Offset propagation across CONFIRMED single-device reset runs, anchored
    #    ONLY by an exact human/local date belonging to the run.
    groups: dict[str, list[ReconstructionInput]] = {}
    for i in inputs:
        if i.reset_group is not None:
            groups.setdefault(i.reset_group, []).append(i)

    for members in groups.values():
        if not all(m.reset_group_strong for m in members):
            continue
        offsets: set[int] = set()
        basis: ReconstructionInput | None = None
        for m in members:
            rd = _recorded_date(m)
            if m.known_true is not None and m.known_true_kind is KnownTrueKind.HUMAN_EXACT \
                    and rd is not None:
                offsets.add((m.known_true - rd).days)
                basis = m
        if len(offsets) != 1:
            continue     # no exact anchor, or conflicting anchors -> withhold
        offset = next(iter(offsets))
        if offset == 0:
            continue
        for m in members:
            if m.file_id in out or m.reliability not in _REVISABLE:
                continue
            rd = _recorded_date(m)
            if rd is None:
                continue
            new = date.fromordinal(rd.toordinal() + offset)
            out[m.file_id] = Reconstruction(
                m.file_id, new, None, Confidence.STRONG, "offset",
                f"Clock-reset offset of {offset} day(s) from the exact date on "
                f"{basis.file_id}; monotonic same-device run.")

    # 3. Range anchors.
    for i in inputs:
        if i.file_id in out or i.anchor_range is None:
            continue
        s, e = i.anchor_range
        out[i.file_id] = Reconstruction(
            i.file_id, s, e, Confidence.RANGE, "anchor_range",
            f"Within the anchored window {s.isoformat()}…{e.isoformat()}.")

    # 4. Bracketing — ONLY within a strong single-device ordering. Endpoints must
    #    be point dates (a fuzzy GPS range can't bound a bracket).
    for members in groups.values():
        if not all(m.reset_group_strong for m in members):
            continue     # model-only ordering can't place frame N chronologically
        ordered = sorted((m for m in members if m.seq is not None), key=lambda m: m.seq)
        points = [(m.seq, out[m.file_id].start) for m in ordered
                  if m.file_id in out and out[m.file_id].end is None]
        if len(points) < 2:
            continue
        for m in ordered:
            if m.file_id in out or m.reliability not in _REVISABLE:
                continue
            earlier = [d for (s, d) in points if s < m.seq]
            later = [d for (s, d) in points if s > m.seq]
            if not earlier or not later:
                continue
            lo, hi = max(earlier), min(later)
            if lo > hi:
                continue
            out[m.file_id] = Reconstruction(
                m.file_id, lo, hi if hi != lo else None,
                Confidence.RANGE if hi != lo else Confidence.STRONG, "bracket",
                f"Between point-dated neighbours {lo.isoformat()} and {hi.isoformat()} "
                "in a confirmed single-device sequence.")

    return out
