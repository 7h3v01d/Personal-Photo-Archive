"""Phase 8.2 — scale, density, and bounded-window helpers for Timeline.

Pure presentation helpers over an immutable :class:`TimelineView`.  This module
never re-evaluates chronology and never mutates archive state.  It provides:

* decade/year/month density buckets;
* deterministic fast-jump targets;
* bounded paging windows so a UI never needs to materialise an entire archive.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from ppa.timeline import TimelineItem, TimelineView

VALID_SCALES = ("decade", "year", "month")
VALID_LANES = {"placed", "range", "tentative", "unplaced"}
DEFAULT_PAGE_SIZE = 120


@dataclass(frozen=True)
class DensityBucket:
    key: str
    label: str
    scale: str
    count: int
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class TimelinePage:
    items: tuple[TimelineItem, ...]
    page: int
    page_size: int
    total_items: int
    total_pages: int
    start_index: int
    end_index: int

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return self.page + 1 < self.total_pages


def _dated(view: TimelineView, lane: str | None = None) -> list[TimelineItem]:
    if lane is not None and lane not in VALID_LANES:
        raise ValueError(f"unknown timeline lane: {lane!r}")
    return [
        i for i in view.items
        if i.start_date is not None and i.lane != "unplaced" and (lane is None or i.lane == lane)
    ]


def _key_for(item: TimelineItem, scale: str) -> str:
    if not item.start_date:
        raise ValueError("unplaced item has no scale key")
    year = int(item.start_date[:4])
    if scale == "decade":
        return f"{(year // 10) * 10:04d}s"
    if scale == "year":
        return item.start_date[:4]
    if scale == "month":
        return item.start_date[:7]
    raise ValueError(f"unknown timeline scale: {scale!r}")


def density_buckets(view: TimelineView, *, scale: str, lane: str | None = None) -> tuple[DensityBucket, ...]:
    """Aggregate already-placed/tentative items into stable density buckets.

    Ranges are indexed by their start date only for navigation.  The underlying
    TimelineItem remains a range and its end_date is never discarded.
    """
    if scale not in VALID_SCALES:
        raise ValueError(f"unknown timeline scale: {scale!r}")
    grouped: dict[str, list[str]] = {}
    for item in _dated(view, lane):
        grouped.setdefault(_key_for(item, scale), []).append(item.file_id)
    return tuple(
        DensityBucket(key, key, scale, len(ids), tuple(ids))
        for key, ids in sorted(grouped.items())
    )


def filter_for_bucket(view: TimelineView, *, bucket_key: str, lane: str | None = None) -> tuple[TimelineItem, ...]:
    """Return dated items represented by a decade/year/month density key."""
    if lane is not None and lane not in VALID_LANES:
        raise ValueError(f"unknown timeline lane: {lane!r}")

    if len(bucket_key) == 5 and bucket_key.endswith("s") and bucket_key[:4].isdigit():
        decade = int(bucket_key[:4])
        pred = lambda i: decade <= int(i.start_date[:4]) <= decade + 9
    elif len(bucket_key) == 4 and bucket_key.isdigit():
        pred = lambda i: i.start_date.startswith(bucket_key)
    elif len(bucket_key) == 7 and bucket_key[4] == "-" and bucket_key[:4].isdigit():
        pred = lambda i: i.start_date.startswith(bucket_key)
    else:
        raise ValueError(f"unknown timeline bucket: {bucket_key!r}")

    return tuple(i for i in _dated(view, lane) if pred(i))


def page_items(items, *, page: int = 0, page_size: int = DEFAULT_PAGE_SIZE) -> TimelinePage:
    """Return a deterministic bounded page without copying more than necessary."""
    if page_size < 1 or page_size > 1000:
        raise ValueError("page_size must be between 1 and 1000")
    seq = tuple(items)
    total = len(seq)
    total_pages = max(1, ceil(total / page_size))
    if page < 0 or page >= total_pages:
        raise ValueError(f"page {page} outside 0..{total_pages - 1}")
    start = page * page_size
    end = min(total, start + page_size)
    return TimelinePage(seq[start:end], page, page_size, total, total_pages, start, end)


def page_for_fraction(total_items: int, fraction: float, *, page_size: int = DEFAULT_PAGE_SIZE) -> int:
    """Map a scrubber fraction [0,1] to a bounded page index."""
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    if page_size < 1:
        raise ValueError("page_size must be positive")
    pages = max(1, ceil(total_items / page_size))
    if pages == 1:
        return 0
    return min(pages - 1, int(round(fraction * (pages - 1))))


def density_peak(buckets: tuple[DensityBucket, ...]) -> int:
    return max((b.count for b in buckets), default=0)
