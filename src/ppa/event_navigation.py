"""Phase 8.8 — deterministic read-only Event-to-Event story navigation.

Human Events already have durable identity and membership.  This module adds a
pure browsing index over those objects; it never changes Event state or
chronology authority.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from sqlite3 import Connection

from ppa.events import Event, list_events

EVENT_BROWSE_SCHEMA = "ppa-event-browse/1"


@dataclass(frozen=True)
class EventCard:
    event_id: str
    name: str
    start_date: str
    end_date: str
    member_count: int
    year: int
    position: int
    previous_event_id: str | None
    next_event_id: str | None

    @property
    def date_label(self) -> str:
        return self.start_date if self.start_date == self.end_date else f"{self.start_date} → {self.end_date}"


@dataclass(frozen=True)
class EventYearGroup:
    year: int
    event_ids: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class EventBrowseIndex:
    schema: str
    read_only: bool
    library_id: int
    cards: tuple[EventCard, ...]
    years: tuple[EventYearGroup, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )

    def card(self, event_id: str) -> EventCard:
        for card in self.cards:
            if card.event_id == event_id:
                return card
        raise ValueError(f"event not present in browse index: {event_id}")


def build_event_browse_index(conn: Connection, *, library_id: int) -> EventBrowseIndex:
    """Build the stable chronological reading order for one Library's Events."""
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    events: tuple[Event, ...] = list_events(conn, library_id=library_id)
    cards: list[EventCard] = []
    for pos, event in enumerate(events):
        cards.append(EventCard(
            event.id,
            event.name,
            event.start_date,
            event.end_date,
            len(event.file_ids),
            int(event.start_date[:4]),
            pos,
            events[pos - 1].id if pos > 0 else None,
            events[pos + 1].id if pos + 1 < len(events) else None,
        ))

    groups: list[EventYearGroup] = []
    by_year: dict[int, list[str]] = {}
    for card in cards:
        by_year.setdefault(card.year, []).append(card.event_id)
    for year in sorted(by_year):
        ids = tuple(by_year[year])
        groups.append(EventYearGroup(year, ids, len(ids)))

    return EventBrowseIndex(EVENT_BROWSE_SCHEMA, True, library_id, tuple(cards), tuple(groups))


def concise_text(index: EventBrowseIndex) -> str:
    lines = [
        "PPA Event Browse",
        "================",
        f"Library: {index.library_id}",
        f"Events: {len(index.cards)}",
    ]
    for group in index.years:
        lines += ["", f"{group.year} ({group.count})", "-" * (len(str(group.year)) + len(str(group.count)) + 3)]
        for event_id in group.event_ids:
            card = index.card(event_id)
            lines.append(f"{card.date_label:24} {card.name} ({card.member_count} photos)")
    return "\n".join(lines)
