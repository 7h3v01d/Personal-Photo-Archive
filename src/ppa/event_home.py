"""Phase 8.9 — read-only Family History / Event Home projection.

This module composes durable human Events, their narrative context, and the
already-authorised Timeline projection into deterministic visual Event cards.
It never changes Event state or chronology authority.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sqlite3 import Connection

from ppa.event_navigation import build_event_browse_index
from ppa.event_story import build_event_story
from ppa.events import get_event_presentation
from ppa.timeline import TimelineView

EVENT_HOME_SCHEMA = "ppa-event-home/1"


@dataclass(frozen=True)
class EventHomeCard:
    event_id: str
    name: str
    start_date: str
    end_date: str
    year: int
    position: int
    member_count: int
    visible_member_count: int
    cover_file_id: str | None
    cover_rule: str
    snippet: str
    occasion_text: str | None
    place_text: str | None
    lane_counts: dict[str, int]
    favorite: bool
    last_viewed_at: str | None
    view_count: int

    @property
    def date_label(self) -> str:
        return self.start_date if self.start_date == self.end_date else f"{self.start_date} → {self.end_date}"


@dataclass(frozen=True)
class EventHomeYear:
    year: int
    event_ids: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class EventHomeView:
    schema: str
    read_only: bool
    library_id: int
    cards: tuple[EventHomeCard, ...]
    years: tuple[EventHomeYear, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))

    def card(self, event_id: str) -> EventHomeCard:
        for card in self.cards:
            if card.event_id == event_id:
                return card
        raise ValueError(f"event not present in Family History view: {event_id}")


def _snippet(description: str | None, story: str | None, note: str | None,
             occasion: str | None, place: str | None, *, limit: int = 220) -> str:
    text = description or story or note or " · ".join(x for x in (occasion, place) if x) or ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _cover_file_id(conn: Connection, story) -> tuple[str | None, str]:
    pref = get_event_presentation(conn, story.event.id)
    visible_ids = {p.file_id for p in story.photos}
    if pref.cover_file_id is not None and pref.cover_file_id in visible_ids:
        return pref.cover_file_id, "human_preferred_member"
    # Cover selection is intentionally semantically neutral. Prefer an original
    # seed solely because it was part of the Event at creation; choose by stable
    # file identity, not chronology, face content, or visual judgement.
    seed_ids = sorted(p.file_id for p in story.photos if p.member_role == "authoritative_seed")
    if seed_ids:
        return seed_ids[0], "stable_authoritative_seed_file_id"
    visible = sorted(p.file_id for p in story.photos)
    if visible:
        return visible[0], "stable_visible_member_file_id"
    return None, "no_visible_member"


def build_event_home(conn: Connection, view: TimelineView) -> EventHomeView:
    """Build a deterministic read-only landing page for one Timeline library."""
    index = build_event_browse_index(conn, library_id=view.scope.library_id)
    before = conn.total_changes
    cards: list[EventHomeCard] = []
    activity_rows = conn.execute(
        "SELECT event_id,favorite,last_viewed_at,view_count FROM event_navigation_state WHERE library_id=?",
        (view.scope.library_id,),
    ).fetchall()
    activity = {r["event_id"]: r for r in activity_rows}
    for nav in index.cards:
        story = build_event_story(conn, view, nav.event_id)
        cover, rule = _cover_file_id(conn, story)
        ctx = story.context
        nav_state = activity.get(nav.event_id)
        cards.append(EventHomeCard(
            nav.event_id, nav.name, nav.start_date, nav.end_date, nav.year, nav.position,
            nav.member_count, len(story.photos), cover, rule,
            _snippet(ctx.description, ctx.story_text, story.event.note,
                     ctx.occasion_text, ctx.place_text),
            ctx.occasion_text, ctx.place_text, dict(story.lane_counts),
            bool(nav_state["favorite"]) if nav_state else False,
            nav_state["last_viewed_at"] if nav_state else None,
            nav_state["view_count"] if nav_state else 0,
        ))
    if conn.total_changes != before:
        raise RuntimeError("Family History projection must be read-only")
    years = tuple(EventHomeYear(g.year, g.event_ids, g.count) for g in index.years)
    return EventHomeView(EVENT_HOME_SCHEMA, True, view.scope.library_id, tuple(cards), years)


def concise_text(home: EventHomeView) -> str:
    lines = ["PPA Family History", "==================", f"Library: {home.library_id}", f"Events: {len(home.cards)}"]
    for year in home.years:
        lines += ["", f"{year.year} ({year.count})", "-" * (len(str(year.year)) + len(str(year.count)) + 3)]
        for eid in year.event_ids:
            c = home.card(eid)
            extra = f" — {c.snippet}" if c.snippet else ""
            lines.append(f"{c.date_label:24} {c.name} ({c.member_count} photos){extra}")
    return "\n".join(lines)
