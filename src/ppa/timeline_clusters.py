"""Phase 8.3 — conservative chronological clustering for Timeline.

Clusters are browsing context, not historical claims.  Only authoritative point
placements (Timeline lane ``placed`` with no end date) can seed or enlarge a
cluster.  Ranges and tentative proposals may be shown as nearby context, but can
never create an event-like group or increase its authoritative photo count.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from ppa.timeline import TimelineItem, TimelineView

CLUSTERS_SCHEMA = "ppa-timeline-clusters/1"


@dataclass(frozen=True)
class DayCount:
    day: str
    count: int


@dataclass(frozen=True)
class TimelineCluster:
    key: str
    kind: str  # day_burst | dense_multi_day_run
    label: str
    start_date: str
    end_date: str
    authoritative_count: int
    seed_file_ids: tuple[str, ...]
    context_file_ids: tuple[str, ...]
    day_counts: tuple[DayCount, ...]
    reason: str

    @property
    def all_file_ids(self) -> tuple[str, ...]:
        return self.seed_file_ids + tuple(x for x in self.context_file_ids if x not in self.seed_file_ids)


@dataclass(frozen=True)
class TimelineClusters:
    schema: str
    generated_at: str
    read_only: bool
    clusters: tuple[TimelineCluster, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


def _day(s: str) -> date:
    return date.fromisoformat(s)


def _stable_key(kind: str, start: str, end: str, ids: tuple[str, ...]) -> str:
    payload = "\n".join((kind, start, end, *ids)).encode("utf-8")
    return "cluster-" + hashlib.sha256(payload).hexdigest()[:16]


def _intersects(item: TimelineItem, start: date, end: date) -> bool:
    if not item.start_date or item.lane == "unplaced":
        return False
    a = _day(item.start_date)
    b = _day(item.end_date) if item.end_date else a
    return a <= end and b >= start


def build_clusters(view: TimelineView, *, min_same_day: int = 4,
                   min_multi_day_daily: int = 2, min_multi_day_total: int = 8,
                   max_multi_day_span: int = 7,
                   generated_at: str | None = None) -> TimelineClusters:
    """Build stable provisional browsing clusters from an immutable TimelineView.

    Same-day bursts require ``min_same_day`` authoritative point placements.
    Dense multi-day runs require 2..``max_multi_day_span`` consecutive calendar
    days, each with at least ``min_multi_day_daily`` authoritative placements and
    at least ``min_multi_day_total`` photos total.  Runs longer than the maximum
    are deliberately not promoted into one giant pseudo-event.
    """
    if min_same_day < 2 or min_multi_day_daily < 1 or min_multi_day_total < 2:
        raise ValueError("cluster thresholds are too small")
    if max_multi_day_span < 2:
        raise ValueError("max_multi_day_span must be at least 2")

    authoritative = [
        i for i in view.items
        if i.lane == "placed" and i.start_date is not None and i.end_date is None
    ]
    by_day: dict[date, list[TimelineItem]] = {}
    for item in authoritative:
        by_day.setdefault(_day(item.start_date), []).append(item)
    for vals in by_day.values():
        vals.sort(key=lambda i: (i.filename.casefold(), i.file_id))

    # Candidate consecutive dense-day runs.  Long runs are intentionally left as
    # individual day bursts rather than being split into arbitrary pseudo-events.
    dense_days = sorted(d for d, vals in by_day.items() if len(vals) >= min_multi_day_daily)
    runs: list[list[date]] = []
    current: list[date] = []
    for d in dense_days:
        if current and d != current[-1] + timedelta(days=1):
            runs.append(current); current = []
        current.append(d)
    if current:
        runs.append(current)

    cluster_specs: list[tuple[str, date, date, tuple[str, ...], tuple[DayCount, ...], str]] = []
    covered_days: set[date] = set()
    for run in runs:
        if not (2 <= len(run) <= max_multi_day_span):
            continue
        ids = tuple(i.file_id for d in run for i in by_day[d])
        if len(ids) < min_multi_day_total:
            continue
        counts = tuple(DayCount(d.isoformat(), len(by_day[d])) for d in run)
        cluster_specs.append((
            "dense_multi_day_run", run[0], run[-1], ids, counts,
            "Several adjacent days contain sustained authoritative photo activity; grouped only as provisional browsing context.",
        ))
        covered_days.update(run)

    # Same-day bursts are useful when a day is not already represented by a
    # qualifying multi-day run.
    for d in sorted(by_day):
        vals = by_day[d]
        if d in covered_days or len(vals) < min_same_day:
            continue
        ids = tuple(i.file_id for i in vals)
        cluster_specs.append((
            "day_burst", d, d, ids, (DayCount(d.isoformat(), len(vals)),),
            "Many authoritative photos share the same calendar day; this is a provisional chronological burst, not a named event.",
        ))

    clusters: list[TimelineCluster] = []
    for kind, start, end, seed_ids, counts, reason in cluster_specs:
        seed_ids = tuple(sorted(seed_ids))
        context = tuple(sorted(
            i.file_id for i in view.items
            if i.file_id not in seed_ids and _intersects(i, start, end)
        ))
        start_s, end_s = start.isoformat(), end.isoformat()
        label = (f"{start_s} · {len(seed_ids)} photos" if start == end else
                 f"{start_s} → {end_s} · {len(seed_ids)} photos")
        clusters.append(TimelineCluster(
            _stable_key(kind, start_s, end_s, seed_ids), kind, label,
            start_s, end_s, len(seed_ids), seed_ids, context, counts, reason,
        ))

    clusters.sort(key=lambda c: (c.start_date, c.end_date, c.kind, c.key))
    return TimelineClusters(
        CLUSTERS_SCHEMA,
        generated_at or datetime.now().astimezone().isoformat(),
        True,
        tuple(clusters),
    )



def items_for_cluster(view: TimelineView, cluster: TimelineCluster, *, lane: str | None = None) -> tuple[TimelineItem, ...]:
    """Return cluster seed/context items while preserving their original lanes."""
    valid = {"placed", "range", "tentative", "unplaced"}
    if lane is not None and lane not in valid:
        raise ValueError(f"unknown timeline lane: {lane!r}")
    allowed = set(cluster.all_file_ids)
    return tuple(
        i for i in view.items
        if i.file_id in allowed and (lane is None or i.lane == lane)
    )

def concise_text(result: TimelineClusters) -> str:
    lines = [
        "PPA Timeline — provisional chronological clusters",
        "=================================================",
        f"Clusters: {len(result.clusters)}",
        "",
    ]
    if not result.clusters:
        lines.append("No conservative chronological clusters detected.")
    for c in result.clusters:
        extra = f" + {len(c.context_file_ids)} contextual" if c.context_file_ids else ""
        lines.append(f"{c.label} [{c.kind}] · {c.authoritative_count} authoritative{extra}")
    lines += ["", "Read-only: clusters are browsing context, not event/date evidence."]
    return "\n".join(lines)
