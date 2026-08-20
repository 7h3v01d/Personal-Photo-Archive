"""Date Reliability Engine — Phase 6, Slice 1 (intrinsic signals).

Rates how much a photograph's *own* recorded timestamp can be believed, from
that photo's evidence alone — no cross-photo sequence reasoning yet (Slice 2).
Read-only and deterministic: it never changes a photo, an observation, or a
stored date. It reports a rating plus the source-qualified evidence and the
reasons behind it, so uncertainty is represented, not hidden.

Design principles (hard-won from adversarial review):

  * **Provenance is evidence.** Every signal carries its (source, key). A value
    labelled DateTimeOriginal by something other than EXIF is NOT EXIF evidence.
    Only an explicit allow-list of (source, key) pairs is consulted.
  * **Repetition is not independent corroboration.** DateTimeOriginal and
    DateTimeDigitized are usually written by the same camera from the same
    (possibly wrong) clock. Matching fields do not make a date *trusted*; at
    most they leave it plausible. Intrinsic evidence therefore never yields
    TRUSTED — that is reserved for genuinely independent evidence introduced
    later (human confirmation, anchors, independent GPS time).
  * **Contradiction never increases confidence.** Disagreeing date fields
    downgrade the rating and appear as evidence.
  * **Don't invent certainty.** A camera-reset epoch is a *suspicion*, not a
    proof; a timezone-less capture time slightly ahead of UTC is not "the
    future". These stay conservative until Slice 2 supplies corroborating
    cross-photo evidence.

Ratings:

    PROBABLY_VALID  a clean, plausible DateTimeOriginal (best a lone photo gets)
    QUESTIONABLE    soft doubt: reset epoch, field disagreement, fallback field,
                    malformed value, or only a filesystem date
    LIKELY_WRONG    impossible: comfortably in the future, or before the window
    UNKNOWN         no usable date evidence at all
    TRUSTED         never produced intrinsically (see above)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from sqlite3 import Connection


class Reliability(str, Enum):
    TRUSTED = "TRUSTED"              # reserved; not produced by intrinsic signals
    PROBABLY_VALID = "PROBABLY_VALID"
    QUESTIONABLE = "QUESTIONABLE"
    LIKELY_WRONG = "LIKELY_WRONG"
    UNKNOWN = "UNKNOWN"


class ParseStatus(str, Enum):
    OK = "ok"
    MALFORMED = "malformed"          # supplied, but not a usable timestamp


# Evidence the intrinsic engine is allowed to consult, as (source, key). Any
# observation with another source is ignored for dating, whatever its key.
ALLOWED_EVIDENCE: frozenset[tuple[str, str]] = frozenset({
    ("exif", "DateTimeOriginal"),
    ("exif", "DateTimeDigitized"),
    ("exif", "DateTime"),
    ("filesystem", "mtime"),
})

# EXIF capture-time keys in order of authority.
_EXIF_CAPTURE_KEYS = ("DateTimeOriginal", "DateTimeDigitized", "DateTime")

# Calendar days on which a capture time is *suspected* to be a camera reset /
# clock-never-set epoch. Treated as suspicion (QUESTIONABLE), never certainty —
# a real photo can be taken on these days; Slice 2 escalates with cross-photo
# evidence (many sequential photos anchored to the epoch, contradicted by
# neighbours, etc.).
KNOWN_RESET_EPOCHS: frozenset[tuple[int, int, int]] = frozenset({
    (1980, 1, 1),   # FAT epoch
    (2000, 1, 1),
    (2001, 1, 1),
    (2002, 1, 1),
    (2003, 1, 1),
    (2004, 1, 1),
    (2007, 1, 1),
    (2008, 1, 1),
})

DEFAULT_EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)
# EXIF has no timezone; a local capture time can be up to ~14h ahead of UTC.
# Only treat a capture as "the future" when it is comfortably beyond that.
DEFAULT_FUTURE_TOLERANCE = timedelta(hours=48)
# How far two EXIF capture fields may differ before it counts as disagreement.
_FIELD_AGREEMENT_TOLERANCE = timedelta(days=1)
# A filesystem mtime this far *before* the claimed capture time is
# chronologically unusual (a file can't ordinarily be modified before it
# exists). Generous, in days, to absorb timezone/clock noise. The reverse
# (mtime after capture) is normal — copies/edits happen later — and is ignored.
_MTIME_BEFORE_CAPTURE_TOLERANCE = timedelta(days=2)


@dataclass(frozen=True)
class DateObservation:
    """A source-qualified dated observation fed to the engine."""
    source: str   # e.g. "exif", "filesystem"
    key: str      # e.g. "DateTimeOriginal", "mtime"
    value: str


@dataclass
class DateSignal:
    """One dated piece of evidence, with provenance and parse status."""
    source: str
    key: str
    raw: str
    parsed: datetime | None
    status: ParseStatus = ParseStatus.OK
    note: str = ""


@dataclass
class DateAssessment:
    reliability: Reliability
    candidate_date: datetime | None    # best observed candidate, NOT an interpreted
    #                                    capture date (Phase 7 produces that)
    signals: list[DateSignal] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def parse_exif_datetime(raw: str) -> datetime | None:
    """Parse an EXIF datetime string ("YYYY:MM:DD HH:MM:SS").

    EXIF carries no timezone, so the result is naive-as-UTC for comparison only;
    it is never presented as an absolute instant. Returns None if unparseable or
    a zeroed placeholder.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("0000"):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_fs_datetime(raw: str) -> datetime | None:
    """Parse a filesystem-mtime observation (ISO 8601, possibly 'Z')."""
    if not raw:
        return None
    raw = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_reset_epoch(dt: datetime) -> bool:
    return (dt.year, dt.month, dt.day) in KNOWN_RESET_EPOCHS


def _fmt_days(delta: timedelta) -> str:
    return f"{abs(delta.days)} day(s)"


def _normalise_observations(observations) -> list[DateObservation]:
    """Accept a list of DateObservation or a {(source, key): value} mapping.

    A flat {key: value} dict is rejected: it has no provenance, and silently
    treating it as EXIF is exactly the masquerade this engine must prevent.
    """
    if isinstance(observations, dict):
        out: list[DateObservation] = []
        for k, v in observations.items():
            if not (isinstance(k, tuple) and len(k) == 2):
                raise TypeError(
                    "assess() requires source-qualified observations: a list of "
                    "DateObservation or a {(source, key): value} mapping, not a "
                    "flat {key: value} dict (that would discard provenance)."
                )
            out.append(DateObservation(k[0], k[1], v))
        return out
    return [o if isinstance(o, DateObservation) else DateObservation(*o)
            for o in observations]


def assess(
    observations,
    *,
    now: datetime | None = None,
    earliest: datetime | None = None,
    future_tolerance: timedelta | None = None,
) -> DateAssessment:
    """Assess one photo's date reliability from source-qualified observations.

    ``observations`` may be a list of :class:`DateObservation`, or a mapping of
    (source, key) -> value. A plain ``dict[str, str]`` of key -> value is NOT
    accepted, because that would discard provenance.

    Pure and deterministic; produces at most PROBABLY_VALID.
    """
    now = now or datetime.now(timezone.utc)
    earliest = earliest or DEFAULT_EARLIEST
    future_tolerance = future_tolerance if future_tolerance is not None else DEFAULT_FUTURE_TOLERANCE

    obs = _normalise_observations(observations)

    signals: list[DateSignal] = []
    reasons: list[str] = []

    parsed_by_sk: dict[tuple[str, str], list[datetime]] = {}
    reps_by_sk: dict[tuple[str, str], list[tuple[str, str]]] = {}   # canonical reps
    disp_by_sk: dict[tuple[str, str], list[str]] = {}               # for messages
    exif_malformed: set[str] = set()

    def _record(source: str, key: str, parsed: datetime | None, raw: str) -> None:
        if parsed is not None:
            parsed_by_sk.setdefault((source, key), []).append(parsed)
            rep, disp = ("v", parsed.isoformat()), parsed.isoformat()
        else:
            rep, disp = ("m", raw.strip()), repr(raw)
        reps_by_sk.setdefault((source, key), []).append(rep)
        disp_by_sk.setdefault((source, key), []).append(disp)

    for o in obs:
        if (o.source, o.key) not in ALLOWED_EVIDENCE:
            continue  # provenance: only authorised (source, key) evidence counts
        if o.source == "exif":
            parsed = parse_exif_datetime(o.value)
            status = ParseStatus.OK if parsed is not None else ParseStatus.MALFORMED
            note = "" if parsed is not None else "present but not a usable timestamp"
            signals.append(DateSignal("exif", o.key, o.value, parsed, status, note))
            if parsed is None:
                exif_malformed.add(o.key)
            _record("exif", o.key, parsed, o.value)
        elif o.source == "filesystem" and o.key == "mtime":
            parsed = parse_fs_datetime(o.value)
            status = ParseStatus.OK if parsed is not None else ParseStatus.MALFORMED
            signals.append(DateSignal("filesystem", "mtime", o.value, parsed, status))
            _record("filesystem", "mtime", parsed, o.value)

    # Conflicting duplicate evidence: the same (source, key) supplied more than
    # once with observations that are not all semantically identical — differing
    # values, OR a mix of valid and malformed. Redundant identical repeats are
    # fine. Ambiguous evidence must never be silently resolved by insertion order.
    conflicts = [sk for sk, reps in reps_by_sk.items() if len(set(reps)) > 1]

    # Representative (first-seen) value per key; deterministic, order-stable.
    exif_parsed: dict[str, datetime] = {
        k: v[0] for (s, k), v in parsed_by_sk.items() if s == "exif"
    }
    fs_vals = parsed_by_sk.get(("filesystem", "mtime"))
    fs_dt: datetime | None = fs_vals[0] if fs_vals else None

    primary_key = next((k for k in _EXIF_CAPTURE_KEYS if k in exif_parsed), None)
    primary = exif_parsed.get(primary_key) if primary_key else None

    if "DateTimeOriginal" in exif_malformed:
        reasons.append("DateTimeOriginal is present but malformed (unusable).")

    # No usable EXIF capture time.
    if primary is None:
        if fs_dt is not None:
            reasons.append("No EXIF capture date; only a filesystem mtime, which is "
                           "not a capture time and drifts on copy/restore.")
            return DateAssessment(Reliability.QUESTIONABLE, fs_dt, signals, reasons)
        reasons.append("No usable date evidence.")
        return DateAssessment(Reliability.UNKNOWN, None, signals, reasons)

    # Hard-impossible checks -> LIKELY_WRONG.
    if primary > now + future_tolerance:
        reasons.append(f"Capture time {primary.isoformat()} is beyond now "
                       f"(+{future_tolerance}); comfortably in the future.")
        return DateAssessment(Reliability.LIKELY_WRONG, primary, signals, reasons)
    if primary < earliest:
        reasons.append(f"Capture time {primary.isoformat()} is before the plausible "
                       f"window (< {earliest.date()}).")
        return DateAssessment(Reliability.LIKELY_WRONG, primary, signals, reasons)

    # Soft doubts -> QUESTIONABLE (any one is enough; collect all reasons).
    questionable = False

    if conflicts:
        questionable = True
        for (s, k) in conflicts:
            vals = ", ".join(sorted(set(disp_by_sk[(s, k)])))
            reasons.append(f"Conflicting {s}:{k} observations ({vals}); ambiguous evidence.")

    # Filesystem mtime materially BEFORE the claimed capture time is unusual (a
    # file can't ordinarily be modified before it exists). Weak evidence, so a
    # downgrade — not a verdict. The reverse direction is normal and ignored.
    if fs_dt is not None and fs_dt < primary - _MTIME_BEFORE_CAPTURE_TOLERANCE:
        questionable = True
        reasons.append(
            f"Filesystem mtime predates the claimed capture date by "
            f"{_fmt_days(primary - fs_dt)}. Filesystem timestamps are weak evidence, "
            "but this is chronologically unusual."
        )

    if "DateTimeOriginal" in exif_parsed and "DateTimeDigitized" in exif_parsed:
        delta = exif_parsed["DateTimeDigitized"] - exif_parsed["DateTimeOriginal"]
        if abs(delta) > _FIELD_AGREEMENT_TOLERANCE:
            questionable = True
            reasons.append("EXIF capture-date fields disagree "
                           f"(DateTimeOriginal vs DateTimeDigitized by {_fmt_days(delta)}).")

    if _is_reset_epoch(primary):
        questionable = True
        reasons.append(f"Capture date {primary.date()} matches a common camera "
                       "reset/clock-never-set epoch (suspicion, not proof).")

    if primary_key != "DateTimeOriginal":
        questionable = True
        reasons.append(f"No usable DateTimeOriginal; capture time taken from {primary_key}.")

    if questionable:
        return DateAssessment(Reliability.QUESTIONABLE, primary, signals, reasons)

    # A clean, plausible DateTimeOriginal. Matching DateTimeDigitized may be
    # noted, but it is the same clock — plausible, not trusted.
    if "DateTimeDigitized" in exif_parsed:
        reasons.append("DateTimeOriginal agrees with DateTimeDigitized (same camera "
                       "clock — corroborating, not independent).")
    reasons.append("Plausible DateTimeOriginal; no intrinsic contradiction found.")
    return DateAssessment(Reliability.PROBABLY_VALID, primary, signals, reasons)


def assess_file(conn: Connection, file_id: str, **kwargs) -> DateAssessment:
    """Assess the photograph at ``file_id`` from its current revision's
    observations. Read-only. Only source-qualified, allow-listed evidence is
    consulted, so a non-EXIF observation can never masquerade as EXIF.
    """
    rows = conn.execute(
        "SELECT source, key, value FROM metadata_observations "
        "WHERE file_id = ? "
        "AND file_revision_id = (SELECT current_revision_id FROM files WHERE id = ?)",
        (file_id, file_id),
    ).fetchall()
    obs = [DateObservation(r["source"], r["key"], r["value"]) for r in rows]
    return assess(obs, **kwargs)
