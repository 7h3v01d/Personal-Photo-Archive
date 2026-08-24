"""Phase 8.13 — lightweight Event favourites and recent-view navigation state.

This state is presentation/navigation metadata only. It never contributes to
chronology, Event membership, reconstruction, or photographic evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection

RECENT_LIMIT = 100

@dataclass(frozen=True)
class EventActivity:
    event_id: str
    library_id: int
    favorite: bool
    last_viewed_at: str | None
    view_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_library(conn: Connection, event_id: str) -> int:
    row = conn.execute("SELECT library_id FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"event not found: {event_id}")
    return int(row["library_id"])


def get_event_activity(conn: Connection, event_id: str) -> EventActivity:
    library_id = _event_library(conn, event_id)
    row = conn.execute("SELECT * FROM event_navigation_state WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        return EventActivity(event_id, library_id, False, None, 0)
    return EventActivity(row["event_id"], row["library_id"], bool(row["favorite"]), row["last_viewed_at"], row["view_count"])


def set_event_favorite(conn: Connection, event_id: str, favorite: bool) -> EventActivity:
    library_id = _event_library(conn, event_id); now = _now()
    conn.execute(
        "INSERT INTO event_navigation_state(event_id,library_id,favorite,last_viewed_at,view_count,updated_at) "
        "VALUES (?,?,?,NULL,0,?) ON CONFLICT(event_id) DO UPDATE SET favorite=excluded.favorite, updated_at=excluded.updated_at",
        (event_id, library_id, int(bool(favorite)), now),
    )
    conn.commit()
    return get_event_activity(conn, event_id)


def record_event_view(conn: Connection, event_id: str, *, viewed_at: str | None = None,
                      recent_limit: int = RECENT_LIMIT) -> EventActivity:
    if recent_limit < 1:
        raise ValueError("recent_limit must be >= 1")
    library_id = _event_library(conn, event_id); when = viewed_at or _now(); now = _now()
    # Validate caller-supplied timestamps while retaining their exact text.
    datetime.fromisoformat(when.replace("Z", "+00:00"))
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO event_navigation_state(event_id,library_id,favorite,last_viewed_at,view_count,updated_at) "
            "VALUES (?,?,0,?,1,?) ON CONFLICT(event_id) DO UPDATE SET "
            "last_viewed_at=excluded.last_viewed_at, view_count=event_navigation_state.view_count+1, updated_at=excluded.updated_at",
            (event_id, library_id, when, now),
        )
        # Bound only non-favourite recency rows. Favourite Events remain durable,
        # but may lose old recency metadata once outside the retained window.
        old = conn.execute(
            "SELECT event_id FROM event_navigation_state WHERE library_id=? AND last_viewed_at IS NOT NULL "
            "ORDER BY last_viewed_at DESC, event_id LIMIT -1 OFFSET ?", (library_id, recent_limit)
        ).fetchall()
        for row in old:
            conn.execute(
                "UPDATE event_navigation_state SET last_viewed_at=NULL, updated_at=? WHERE event_id=?",
                (now, row["event_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event_activity(conn, event_id)


def list_favorite_event_ids(conn: Connection, *, library_id: int) -> tuple[str, ...]:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    rows = conn.execute(
        "SELECT event_id FROM event_navigation_state WHERE library_id=? AND favorite=1 ORDER BY event_id",
        (library_id,),
    ).fetchall()
    return tuple(r["event_id"] for r in rows)


def list_recent_event_ids(conn: Connection, *, library_id: int, limit: int = 20) -> tuple[str, ...]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    rows = conn.execute(
        "SELECT event_id FROM event_navigation_state WHERE library_id=? AND last_viewed_at IS NOT NULL "
        "ORDER BY last_viewed_at DESC, event_id LIMIT ?", (library_id, limit)
    ).fetchall()
    return tuple(r["event_id"] for r in rows)


def continue_event_id(conn: Connection, *, library_id: int) -> str | None:
    ids = list_recent_event_ids(conn, library_id=library_id, limit=1)
    return ids[0] if ids else None
