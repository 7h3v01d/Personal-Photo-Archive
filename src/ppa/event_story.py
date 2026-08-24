"""Phase 8.7 — read-only Event story projection.

A Story View combines a durable human-authored Event and its narrative context
with the *current* Phase-8 Timeline state of each explicit Event member.

The projection never turns Event narrative or membership into chronology
authority.  A human-added photo that is currently unplaced remains unplaced;
a range remains a range; stale chronology remains visible as stale.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sqlite3 import Connection

from ppa.events import Event, EventContext, get_event, get_event_context, list_event_members, get_event_presentation
from ppa.timeline import TimelineItem, TimelineView

EVENT_STORY_SCHEMA = "ppa-event-story/1"


@dataclass(frozen=True)
class EventStoryPhoto:
    file_id: str
    filename: str
    member_role: str
    member_added_at: str
    lane: str
    source: str
    start_date: str | None
    end_date: str | None
    reliability: str
    confidence: str | None
    method: str | None
    reconstruction_status: str | None
    content_stale: bool
    evidence_stale: bool
    reason: str

    @property
    def stale(self) -> bool:
        return self.content_stale or self.evidence_stale


@dataclass(frozen=True)
class EventStoryView:
    schema: str
    read_only: bool
    event: Event
    context: EventContext
    photos: tuple[EventStoryPhoto, ...]
    lane_counts: dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )


def _story_sort_key(photo: EventStoryPhoto):
    # Current chronology drives presentation order.  Unplaced photos are kept
    # at the end rather than being assigned a synthetic date.  Lane is only a
    # tie-breaker for equal date spans and never changes date authority.
    lane_rank = {"placed": 0, "range": 1, "tentative": 2, "unplaced": 3}
    return (
        photo.start_date is None,
        photo.start_date or "9999-12-31",
        photo.end_date or photo.start_date or "9999-12-31",
        lane_rank.get(photo.lane, 99),
        photo.filename.casefold(),
        photo.file_id,
    )


def build_event_story(conn: Connection, view: TimelineView, event_id: str) -> EventStoryView:
    """Build one deterministic, read-only human Event story.

    ``view`` is the already-authorised Phase-8 chronology projection.  Event
    context and membership are presentation/interpretation only and can never
    create a Timeline placement.
    """
    event = get_event(conn, event_id)
    if event.library_id != view.scope.library_id:
        raise ValueError("event does not belong to the Timeline library")

    by_id: dict[str, TimelineItem] = {item.file_id: item for item in view.items}
    members = list_event_members(conn, event_id)
    context = get_event_context(conn, event_id)

    photos: list[EventStoryPhoto] = []
    for member in members:
        item = by_id.get(member.file_id)
        if item is None:
            # The Event identity is durable, but the supplied Timeline scope may
            # intentionally be narrower than the Event.  Do not fabricate a
            # chronology row for a member the current projection cannot prove.
            continue
        photos.append(EventStoryPhoto(
            item.file_id,
            item.filename,
            member.role,
            member.added_at,
            item.lane,
            item.source,
            item.start_date,
            item.end_date,
            item.reliability,
            item.confidence,
            item.method,
            item.reconstruction_status,
            item.content_stale,
            item.evidence_stale,
            item.reason,
        ))

    presentation = get_event_presentation(conn, event_id)
    if presentation.order_file_ids is not None:
        order_index = {fid: n for n, fid in enumerate(presentation.order_file_ids)}
        photos.sort(key=lambda p: order_index.get(p.file_id, len(order_index)))
    else:
        photos.sort(key=_story_sort_key)
    counts = {lane: 0 for lane in ("placed", "range", "tentative", "unplaced")}
    for photo in photos:
        if photo.lane in counts:
            counts[photo.lane] += 1

    return EventStoryView(EVENT_STORY_SCHEMA, True, event, context, tuple(photos), counts)


def concise_text(story: EventStoryView) -> str:
    span = story.event.start_date
    if story.event.end_date != story.event.start_date:
        span = f"{span} -> {story.event.end_date}"
    lines = [
        f"PPA Event Story — {story.event.name}",
        "=" * min(72, max(20, len(story.event.name) + 18)),
        f"Date span: {span}",
        f"Visible members: {len(story.photos)} / {len(story.event.file_ids)}",
        f"Placed: {story.lane_counts['placed']} · Ranges: {story.lane_counts['range']} · "
        f"Tentative: {story.lane_counts['tentative']} · Unplaced: {story.lane_counts['unplaced']}",
    ]
    ctx = story.context
    if ctx.occasion_text:
        lines.append(f"Occasion: {ctx.occasion_text}")
    if ctx.place_text:
        lines.append(f"Place: {ctx.place_text}")
    if ctx.people_text:
        lines.append(f"People: {ctx.people_text}")
    if ctx.description:
        lines += ["", ctx.description]
    if ctx.story_text:
        lines += ["", ctx.story_text]
    lines += ["", "Photos", "------"]
    for photo in story.photos:
        when = photo.start_date or "unplaced"
        if photo.end_date:
            when = f"{when} -> {photo.end_date}"
        lines.append(f"{when:24} {photo.lane:9} {photo.filename} [{photo.member_role}]")
    return "\n".join(lines)
