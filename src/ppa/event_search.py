"""Phase 8.11 — deterministic read-only Event search and discovery.

Search operates only over durable human-authored Event identity/context plus the
existing Family History ordering. Narrative matches are discovery metadata; they
never participate in chronology, reconstruction, or Event membership authority.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from sqlite3 import Connection

from ppa.event_home import EventHomeView
from ppa.events import get_event, get_event_context

EVENT_SEARCH_SCHEMA = "ppa-event-search/1"


@dataclass(frozen=True)
class EventSearchEntry:
    event_id: str
    name: str
    start_date: str
    end_date: str
    year: int
    position: int
    member_count: int
    note: str | None
    description: str | None
    place_text: str | None
    people_text: str | None
    occasion_text: str | None
    story_text: str | None

    @property
    def date_label(self) -> str:
        return self.start_date if self.start_date == self.end_date else f"{self.start_date} → {self.end_date}"


@dataclass(frozen=True)
class EventSearchIndex:
    schema: str
    read_only: bool
    library_id: int
    entries: tuple[EventSearchEntry, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


@dataclass(frozen=True)
class EventSearchHit:
    event_id: str
    name: str
    date_label: str
    member_count: int
    position: int
    score: int
    matched_fields: tuple[str, ...]


@dataclass(frozen=True)
class EventSearchResults:
    schema: str
    read_only: bool
    library_id: int
    query: str
    year: int | None
    start_date: str | None
    end_date: str | None
    occasion: str | None
    place: str | None
    person: str | None
    total: int
    hits: tuple[EventSearchHit, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))



@dataclass(frozen=True)
class FacetValue:
    value: str
    count: int


@dataclass(frozen=True)
class EventSearchFacets:
    years: tuple[FacetValue, ...]
    occasions: tuple[FacetValue, ...]
    places: tuple[FacetValue, ...]
    people: tuple[FacetValue, ...]


def _facet_values(values: list[str]) -> tuple[FacetValue, ...]:
    counts: dict[str, tuple[str, int]] = {}
    for raw in values:
        value = " ".join(raw.split())
        if not value:
            continue
        key = value.casefold()
        display, count = counts.get(key, (value, 0))
        counts[key] = (display, count + 1)
    return tuple(FacetValue(display, count) for _key, (display, count) in
                 sorted(counts.items(), key=lambda kv: (-kv[1][1], kv[1][0].casefold())))


def _people_terms(text: str | None) -> list[str]:
    if not text:
        return []
    # People notes remain human-authored free text. Comma/semicolon/newline are
    # treated as explicit separators; an unsplit value stays one facet phrase.
    normalized = text.replace(";", ",").replace("\n", ",")
    return [" ".join(part.split()) for part in normalized.split(",") if part.strip()]


def build_event_search_facets(index: EventSearchIndex) -> EventSearchFacets:
    years = [str(e.year) for e in index.entries]
    occasions = [e.occasion_text for e in index.entries if e.occasion_text]
    places = [e.place_text for e in index.entries if e.place_text]
    people: list[str] = []
    for e in index.entries:
        people.extend(_people_terms(e.people_text))
    return EventSearchFacets(_facet_values(years), _facet_values(occasions),
                             _facet_values(places), _facet_values(people))

_FIELDS: tuple[tuple[str, int], ...] = (
    ("name", 100),
    ("occasion", 70),
    ("place", 65),
    ("people", 60),
    ("description", 40),
    ("story", 30),
    ("note", 20),
)


def _clean_query(value: str | None) -> str:
    return " ".join(str(value or "").split())


def _validate_date(value: str | None, label: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    cleaned = str(value).strip()
    try:
        date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {cleaned!r}") from exc
    return cleaned


def build_event_search_index(conn: Connection, home: EventHomeView) -> EventSearchIndex:
    """Build the in-memory discovery index in existing Family History order."""
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (home.library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {home.library_id}")
    before = conn.total_changes
    entries: list[EventSearchEntry] = []
    for card in home.cards:
        event = get_event(conn, card.event_id)
        if event.library_id != home.library_id:
            raise ValueError("Family History contains a cross-library Event")
        ctx = get_event_context(conn, event.id)
        entries.append(EventSearchEntry(
            event.id, event.name, event.start_date, event.end_date, card.year,
            card.position, event.file_ids.__len__(), event.note,
            ctx.description, ctx.place_text, ctx.people_text, ctx.occasion_text, ctx.story_text,
        ))
    if conn.total_changes != before:
        raise RuntimeError("Event search indexing must be read-only")
    return EventSearchIndex(EVENT_SEARCH_SCHEMA, True, home.library_id, tuple(entries))


def _field_values(entry: EventSearchEntry) -> dict[str, str]:
    return {
        "name": entry.name,
        "occasion": entry.occasion_text or "",
        "place": entry.place_text or "",
        "people": entry.people_text or "",
        "description": entry.description or "",
        "story": entry.story_text or "",
        "note": entry.note or "",
    }


def search_event_index(index: EventSearchIndex, *, text: str | None = None,
                       year: int | None = None, start_date: str | None = None,
                       end_date: str | None = None, occasion: str | None = None,
                       place: str | None = None, person: str | None = None) -> EventSearchResults:
    """Filter/rank Events without changing the index or archive state.

    Query tokens use AND semantics. For each token, the highest-weight matching
    field contributes to the score. Empty queries preserve Family History order.
    Date filters use Event-span overlap and never reinterpret member chronology.
    """
    query = _clean_query(text)
    start = _validate_date(start_date, "start date")
    end = _validate_date(end_date, "end date")
    if start and end and end < start:
        raise ValueError("end date must not precede start date")
    if year is not None and (year < 1 or year > 9999):
        raise ValueError("year must be between 1 and 9999")
    tokens = tuple(t.casefold() for t in query.split())
    occasion_filter = _clean_query(occasion).casefold()
    place_filter = _clean_query(place).casefold()
    person_filter = _clean_query(person).casefold()
    hits: list[EventSearchHit] = []
    for entry in index.entries:
        if year is not None and entry.year != year:
            continue
        if start is not None and entry.end_date < start:
            continue
        if end is not None and entry.start_date > end:
            continue
        values = {name: value.casefold() for name, value in _field_values(entry).items()}
        if occasion_filter and occasion_filter not in values["occasion"]:
            continue
        if place_filter and place_filter not in values["place"]:
            continue
        if person_filter and person_filter not in values["people"]:
            continue
        matched: set[str] = set()
        score = 0
        rejected = False
        for token in tokens:
            token_matches = [(name, weight) for name, weight in _FIELDS if token in values[name]]
            if not token_matches:
                rejected = True
                break
            best = max(weight for _name, weight in token_matches)
            score += best
            matched.update(name for name, _weight in token_matches)
        if rejected:
            continue
        hits.append(EventSearchHit(entry.event_id, entry.name, entry.date_label,
                                   entry.member_count, entry.position, score,
                                   tuple(name for name, _weight in _FIELDS if name in matched)))
    if tokens:
        hits.sort(key=lambda h: (-h.score, h.position, h.name.casefold(), h.event_id))
    else:
        hits.sort(key=lambda h: h.position)
    return EventSearchResults(EVENT_SEARCH_SCHEMA, True, index.library_id, query, year,
                              start, end, _clean_query(occasion) or None,
                              _clean_query(place) or None, _clean_query(person) or None,
                              len(hits), tuple(hits))


def concise_text(results: EventSearchResults) -> str:
    bits = ["PPA Event Search", "================", f"Library: {results.library_id}",
            f"Query: {results.query or '(all events)'}"]
    if results.year is not None:
        bits.append(f"Year: {results.year}")
    if results.start_date or results.end_date:
        bits.append(f"Date filter: {results.start_date or '…'} → {results.end_date or '…'}")
    if results.occasion: bits.append(f"Occasion facet: {results.occasion}")
    if results.place: bits.append(f"Place facet: {results.place}")
    if results.person: bits.append(f"People facet: {results.person}")
    bits.append(f"Matches: {results.total}")
    for hit in results.hits:
        why = f" [{', '.join(hit.matched_fields)}]" if hit.matched_fields else ""
        bits.append(f"{hit.date_label:24} {hit.name} ({hit.member_count} photos){why}")
    return "\n".join(bits)
