"""Phase 8.14 — read-only Event curation health indicators.

These indicators summarise current Event presentation/story/Timeline state only.
They are not chronology evidence and never alter Event or photo authority.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sqlite3 import Connection

from ppa.event_story import build_event_story
from ppa.events import get_event_presentation
from ppa.timeline import TimelineView

EVENT_HEALTH_SCHEMA = "ppa-event-health/1"


@dataclass(frozen=True)
class EventHealth:
    event_id: str
    has_story: bool
    has_context: bool
    custom_cover: bool
    custom_order: bool
    contains_ranges: bool
    contains_tentative: bool
    contains_unplaced: bool
    contains_stale: bool
    hidden_members: int
    needs_chronology_review: bool
    needs_story: bool
    needs_attention: bool
    curation_complete: bool
    badges: tuple[str, ...]


@dataclass(frozen=True)
class EventHealthView:
    schema: str
    read_only: bool
    library_id: int
    events: tuple[EventHealth, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))

    def event(self, event_id: str) -> EventHealth:
        for item in self.events:
            if item.event_id == event_id:
                return item
        raise ValueError(f"event not present in health view: {event_id}")


def _context_flags(context) -> tuple[bool, bool]:
    values = (context.description, context.place_text, context.people_text,
              context.occasion_text, context.story_text)
    has_context = any(bool((v or "").strip()) for v in values)
    # Story means there is actual narrative/description, not merely a place or
    # people facet.  This keeps "Has story" semantically useful.
    has_story = bool((context.description or "").strip() or (context.story_text or "").strip())
    return has_story, has_context


def _make_health(*, event_id: str, has_story: bool, has_context: bool,
                 custom_cover: bool, custom_order: bool, lane_counts: dict[str, int],
                 stale: bool, hidden: int) -> EventHealth:
    needs_chronology = stale or lane_counts["tentative"] > 0 or lane_counts["unplaced"] > 0
    needs_story = not has_story
    complete = has_story and not needs_chronology and hidden == 0
    # A single model-owned predicate prevents UI surfaces from reconstructing
    # an incomplete subset of the completion semantics.  In the current model
    # every non-complete Event deserves attention (missing story, chronology
    # review, or hidden/out-of-scope members).
    needs_attention = not complete

    badges: list[str] = []
    if complete: badges.append("Curation complete")
    if has_story: badges.append("Has story")
    if custom_cover: badges.append("Custom cover")
    if custom_order: badges.append("Custom order")
    if lane_counts["range"]: badges.append("Contains ranges")
    if lane_counts["tentative"]: badges.append("Contains tentative photos")
    if lane_counts["unplaced"]: badges.append("Contains unresolved photos")
    if stale: badges.append("Contains stale chronology")
    if hidden: badges.append("Members outside current Timeline scope")
    if needs_chronology: badges.append("Needs chronology review")
    if needs_story: badges.append("Needs story")

    return EventHealth(
        event_id, has_story, has_context, custom_cover, custom_order,
        lane_counts["range"] > 0, lane_counts["tentative"] > 0,
        lane_counts["unplaced"] > 0, stale, hidden,
        needs_chronology, needs_story, needs_attention, complete, tuple(badges),
    )


def build_event_health(conn: Connection, view: TimelineView, event_id: str) -> EventHealth:
    """Build health for one Event.

    This single-Event helper intentionally reuses the Story projection.  The
    collection-wide ``build_event_health_view`` below uses a batched algorithm
    so Family History does not pay an N+1/O(events×timeline) cost.
    """
    before = conn.total_changes
    story = build_event_story(conn, view, event_id)
    presentation = get_event_presentation(conn, event_id)
    has_story, has_context = _context_flags(story.context)
    stale = any(p.stale for p in story.photos)
    hidden = max(0, len(story.event.file_ids) - len(story.photos))
    health = _make_health(
        event_id=event_id, has_story=has_story, has_context=has_context,
        custom_cover=presentation.cover_file_id is not None,
        custom_order=presentation.order_file_ids is not None,
        lane_counts=story.lane_counts, stale=stale, hidden=hidden,
    )
    if conn.total_changes != before:
        raise RuntimeError("Event health projection must be read-only")
    return health


def build_event_health_view(conn: Connection, view: TimelineView) -> EventHealthView:
    """Build all Event health rows with bounded SQL and one Timeline index.

    Phase 8.14.1 deliberately avoids calling ``build_event_story`` once per
    Event.  That older composition rebuilt the entire Timeline dictionary for
    every Event and issued roughly O(events) groups of SQL queries.  Here we:

    * index the immutable Timeline exactly once;
    * fetch Events/members/context/presentation in four library-scoped SELECTs;
    * derive every health row in memory.
    """
    before = conn.total_changes
    library_id = view.scope.library_id
    timeline_by_id = {item.file_id: item for item in view.items}

    event_rows = conn.execute(
        "SELECT id FROM events WHERE library_id=? ORDER BY start_date,end_date,name COLLATE NOCASE,id",
        (library_id,),
    ).fetchall()
    event_ids = [r["id"] for r in event_rows]
    if not event_ids:
        return EventHealthView(EVENT_HEALTH_SCHEMA, True, library_id, ())

    member_rows = conn.execute(
        "SELECT em.event_id,em.file_id FROM event_members em "
        "JOIN events e ON e.id=em.event_id WHERE e.library_id=? ORDER BY em.event_id,em.file_id",
        (library_id,),
    ).fetchall()
    context_rows = conn.execute(
        "SELECT ec.* FROM event_context ec JOIN events e ON e.id=ec.event_id WHERE e.library_id=?",
        (library_id,),
    ).fetchall()
    presentation_rows = conn.execute(
        "SELECT ep.* FROM event_presentation ep JOIN events e ON e.id=ep.event_id WHERE e.library_id=?",
        (library_id,),
    ).fetchall()

    members: dict[str, list[str]] = {eid: [] for eid in event_ids}
    for row in member_rows:
        members.setdefault(row["event_id"], []).append(row["file_id"])
    contexts = {r["event_id"]: r for r in context_rows}
    presentations = {r["event_id"]: r for r in presentation_rows}

    out: list[EventHealth] = []
    for event_id in event_ids:
        context = contexts.get(event_id)
        if context is None:
            has_story = has_context = False
        else:
            values = (context["description"], context["place_text"], context["people_text"],
                      context["occasion_text"], context["story_text"])
            has_context = any(bool((v or "").strip()) for v in values)
            has_story = bool((context["description"] or "").strip() or
                             (context["story_text"] or "").strip())

        lane_counts = {lane: 0 for lane in ("placed", "range", "tentative", "unplaced")}
        stale = False
        visible = 0
        for file_id in members.get(event_id, ()):
            item = timeline_by_id.get(file_id)
            if item is None:
                continue
            visible += 1
            if item.lane in lane_counts:
                lane_counts[item.lane] += 1
            stale = stale or item.content_stale or item.evidence_stale
        hidden = max(0, len(members.get(event_id, ())) - visible)

        presentation = presentations.get(event_id)
        custom_cover = bool(presentation is not None and presentation["cover_file_id"])
        # A stale order can exist in storage after defensive invalidation; the
        # Events API normally clears it on membership mutation.  For a health
        # badge, non-null order_json means the user has a custom presentation
        # preference recorded for the current Event.
        custom_order = bool(presentation is not None and presentation["order_json"])

        out.append(_make_health(
            event_id=event_id, has_story=has_story, has_context=has_context,
            custom_cover=custom_cover, custom_order=custom_order,
            lane_counts=lane_counts, stale=stale, hidden=hidden,
        ))

    if conn.total_changes != before:
        raise RuntimeError("Event health projection must be read-only")
    return EventHealthView(EVENT_HEALTH_SCHEMA, True, library_id, tuple(out))


def concise_text(health: EventHealthView) -> str:
    lines = ["PPA Event Curation Health", "=========================", f"Library: {health.library_id}",
             f"Events: {len(health.events)}"]
    for item in health.events:
        badges = " · ".join(item.badges) if item.badges else "No attention indicators"
        lines.append(f"{item.event_id}  {badges}")
    return "\n".join(lines)
