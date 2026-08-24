"""Phase 8.12 — durable saved Event discovery presets.

A saved view stores query/filter intent, never cached Event ids. Re-evaluation
always runs against the current read-only Event discovery index.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from sqlite3 import Connection

from ppa.event_search import EventSearchIndex, EventSearchResults, search_event_index


@dataclass(frozen=True)
class SavedEventView:
    id: str
    library_id: int
    name: str
    query_text: str
    year: int | None
    start_date: str | None
    end_date: str | None
    occasion_filter: str | None
    place_filter: str | None
    person_filter: str | None
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_name(value: str) -> str:
    out = " ".join(str(value).split())
    if not out:
        raise ValueError("saved view name must not be blank")
    if len(out) > 120:
        raise ValueError("saved view name is too long")
    return out


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    out = " ".join(str(value).split())
    return out or None


def _date(value: str | None, label: str) -> str | None:
    value = _clean_optional(value)
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    return value


def _row(row) -> SavedEventView:
    return SavedEventView(row["id"], row["library_id"], row["name"], row["query_text"],
                          row["year"], row["start_date"], row["end_date"],
                          row["occasion_filter"], row["place_filter"], row["person_filter"],
                          row["created_at"], row["updated_at"])


def save_event_view(conn: Connection, *, library_id: int, name: str,
                    query_text: str = "", year: int | None = None,
                    start_date: str | None = None, end_date: str | None = None,
                    occasion_filter: str | None = None, place_filter: str | None = None,
                    person_filter: str | None = None) -> SavedEventView:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    name = _clean_name(name)
    query_text = " ".join(str(query_text or "").split())
    start_date = _date(start_date, "start date"); end_date = _date(end_date, "end date")
    if start_date and end_date and end_date < start_date:
        raise ValueError("end date must not precede start date")
    if year is not None and not 1 <= int(year) <= 9999:
        raise ValueError("year must be between 1 and 9999")
    occasion_filter = _clean_optional(occasion_filter)
    place_filter = _clean_optional(place_filter)
    person_filter = _clean_optional(person_filter)
    existing = conn.execute("SELECT * FROM saved_event_views WHERE library_id=? AND name=? COLLATE NOCASE",
                            (library_id, name)).fetchone()
    now = _now()
    if existing:
        conn.execute("UPDATE saved_event_views SET name=?,query_text=?,year=?,start_date=?,end_date=?,occasion_filter=?,place_filter=?,person_filter=?,updated_at=? WHERE id=?",
                     (name, query_text, year, start_date, end_date, occasion_filter, place_filter, person_filter, now, existing["id"]))
        conn.commit(); return get_event_view(conn, existing["id"])
    view_id = str(uuid.uuid4())
    conn.execute("INSERT INTO saved_event_views(id,library_id,name,query_text,year,start_date,end_date,occasion_filter,place_filter,person_filter,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (view_id, library_id, name, query_text, year, start_date, end_date, occasion_filter, place_filter, person_filter, now, now))
    conn.commit(); return get_event_view(conn, view_id)


def get_event_view(conn: Connection, view_id: str) -> SavedEventView:
    row = conn.execute("SELECT * FROM saved_event_views WHERE id=?", (view_id,)).fetchone()
    if row is None:
        raise ValueError(f"saved Event view not found: {view_id}")
    return _row(row)


def list_event_views(conn: Connection, *, library_id: int) -> tuple[SavedEventView, ...]:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    rows = conn.execute("SELECT * FROM saved_event_views WHERE library_id=? ORDER BY name COLLATE NOCASE,id", (library_id,)).fetchall()
    return tuple(_row(r) for r in rows)


def delete_event_view(conn: Connection, view_id: str) -> bool:
    cur = conn.execute("DELETE FROM saved_event_views WHERE id=?", (view_id,))
    conn.commit(); return cur.rowcount > 0


def evaluate_saved_view(index: EventSearchIndex, view: SavedEventView) -> EventSearchResults:
    if index.library_id != view.library_id:
        raise ValueError("saved Event view library mismatch")
    return search_event_index(index, text=view.query_text, year=view.year,
                              start_date=view.start_date, end_date=view.end_date,
                              occasion=view.occasion_filter, place=view.place_filter,
                              person=view.person_filter)
