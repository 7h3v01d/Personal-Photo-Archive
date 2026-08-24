"""Phase 8.1 timeline navigation helpers.

Pure, read-only presentation helpers over :mod:`ppa.timeline`.  They do not
reconcile dates or change timeline authority; they only build deterministic
navigation buckets and filter already-projected TimelineItems.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ppa.timeline import TimelineItem, TimelineView


@dataclass(frozen=True)
class TimelineNavEntry:
    key: str                 # all | YYYY | YYYY-MM | unplaced
    label: str
    count: int
    kind: str                # all | year | month | unplaced


def _dated(items: Iterable[TimelineItem]):
    return [i for i in items if i.start_date is not None and i.lane != "unplaced"]


def build_navigation(view: TimelineView) -> tuple[TimelineNavEntry, ...]:
    """Return stable All/year/month/unplaced navigation entries.

    Counts are photo counts, not claims.  A range is indexed by its start month
    only for navigation; the range itself remains intact and is never collapsed.
    """
    dated = _dated(view.items)
    years: dict[str, list[TimelineItem]] = {}
    months: dict[str, list[TimelineItem]] = {}
    for item in dated:
        year = item.start_date[:4]
        month = item.start_date[:7]
        years.setdefault(year, []).append(item)
        months.setdefault(month, []).append(item)

    out: list[TimelineNavEntry] = [
        TimelineNavEntry("all", "All dated", len(dated), "all"),
    ]
    for year in sorted(years):
        out.append(TimelineNavEntry(year, year, len(years[year]), "year"))
        for month in sorted(k for k in months if k.startswith(year + "-")):
            out.append(TimelineNavEntry(month, month, len(months[month]), "month"))
    unplaced = view.lanes["unplaced"].count
    if unplaced:
        out.append(TimelineNavEntry("unplaced", "Unplaced", unplaced, "unplaced"))
    return tuple(out)


def filter_items(view: TimelineView, *, nav_key: str = "all", lane: str | None = None) -> tuple[TimelineItem, ...]:
    """Filter projected items without changing chronology semantics."""
    items = list(view.items)
    if nav_key == "unplaced":
        items = [i for i in items if i.lane == "unplaced"]
    elif nav_key == "all":
        items = [i for i in items if i.lane != "unplaced" and i.start_date is not None]
    elif len(nav_key) == 4 and nav_key.isdigit():
        items = [i for i in items if i.start_date is not None and i.start_date.startswith(nav_key)]
    elif len(nav_key) == 7 and nav_key[4:5] == "-":
        items = [i for i in items if i.start_date is not None and i.start_date.startswith(nav_key)]
    else:
        raise ValueError(f"unknown timeline navigation key: {nav_key!r}")

    if lane is not None:
        if lane not in {"placed", "range", "tentative", "unplaced"}:
            raise ValueError(f"unknown timeline lane: {lane!r}")
        items = [i for i in items if i.lane == lane]
    return tuple(items)
