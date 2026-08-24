"""Phase 8.4/8.5 — durable, auditable human-authored event identity.

A TimelineCluster is derived browsing context. An Event is a human
interpretation with stable identity and explicit human-controlled membership.
Event curation never alters chronology, metadata observations,
reconstructions, or source photos.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from sqlite3 import Connection

from ppa.timeline import TimelineItem, TimelineView
from ppa.timeline_clusters import TimelineCluster


@dataclass(frozen=True)
class Event:
    id: str
    library_id: int
    name: str
    note: str | None
    start_date: str
    end_date: str
    source_kind: str
    source_cluster_key: str | None
    created_at: str
    updated_at: str
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class EventMember:
    file_id: str
    role: str
    added_at: str


@dataclass(frozen=True)
class EventHistoryEntry:
    id: int
    event_id: str
    action: str
    file_id: str | None
    member_role: str | None
    old_value: str | None
    new_value: str | None
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_name(name: str) -> str:
    value = " ".join(str(name).split())
    if not value:
        raise ValueError("event name must not be blank")
    if len(value) > 200:
        raise ValueError("event name is too long")
    return value


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    value = str(note).strip()
    if not value:
        return None
    if len(value) > 4000:
        raise ValueError("event note is too long")
    return value


def _validate_iso_date(value: str) -> str:
    date.fromisoformat(value)
    return value


def _row_to_event(conn: Connection, row) -> Event:
    members = conn.execute(
        "SELECT file_id FROM event_members WHERE event_id = ? ORDER BY file_id", (row["id"],)
    ).fetchall()
    return Event(
        row["id"], row["library_id"], row["name"], row["note"], row["start_date"],
        row["end_date"], row["source_kind"], row["source_cluster_key"],
        row["created_at"], row["updated_at"], tuple(r["file_id"] for r in members),
    )


def _event_state(event_id: str, library_id: int, name: str, note: str | None,
                 start: str, end: str, cluster_key: str | None, seed_ids: tuple[str, ...]) -> str:
    return json.dumps({
        "id": event_id, "library_id": library_id, "name": name, "note": note,
        "start_date": start, "end_date": end, "source_cluster_key": cluster_key,
        "file_ids": list(seed_ids),
    }, sort_keys=True, separators=(",", ":"))


def create_event_from_cluster(conn: Connection, *, library_id: int,
                              cluster: TimelineCluster, name: str,
                              note: str | None = None) -> Event:
    """Create one durable event from the cluster's authoritative seed members."""
    name = _clean_name(name)
    note = _clean_note(note)
    start, end = _validate_iso_date(cluster.start_date), _validate_iso_date(cluster.end_date)
    seed_ids = tuple(sorted(set(cluster.seed_file_ids)))
    if not seed_ids:
        raise ValueError("cannot create an event from an empty cluster")
    if conn.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    placeholders = ",".join("?" for _ in seed_ids)
    rows = conn.execute(f"SELECT id, library_id FROM files WHERE id IN ({placeholders})", seed_ids).fetchall()
    if len(rows) != len(seed_ids) or any(r["library_id"] != library_id for r in rows):
        raise ValueError("cluster contains missing or cross-library files")
    if conn.execute("SELECT 1 FROM events WHERE library_id = ? AND source_cluster_key = ?",
                    (library_id, cluster.key)).fetchone():
        raise ValueError("this provisional cluster already has a human event")

    now = _now(); event_id = str(uuid.uuid4())
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO events (id, library_id, name, note, start_date, end_date, source_kind, "
            "source_cluster_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'timeline_cluster', ?, ?, ?)",
            (event_id, library_id, name, note, start, end, cluster.key, now, now),
        )
        conn.executemany(
            "INSERT INTO event_members (event_id, file_id, role, added_at) VALUES (?, ?, 'authoritative_seed', ?)",
            [(event_id, fid, now) for fid in seed_ids],
        )
        conn.execute(
            "INSERT INTO event_history(event_id,action,new_value,created_at) VALUES (?,'create',?,?)",
            (event_id, _event_state(event_id, library_id, name, note, start, end, cluster.key, seed_ids), now),
        )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event(conn, event_id)


def get_event(conn: Connection, event_id: str) -> Event:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"event not found: {event_id}")
    return _row_to_event(conn, row)


def list_events(conn: Connection, *, library_id: int) -> tuple[Event, ...]:
    rows = conn.execute(
        "SELECT * FROM events WHERE library_id = ? ORDER BY start_date, end_date, name COLLATE NOCASE, id",
        (library_id,),
    ).fetchall()
    return tuple(_row_to_event(conn, r) for r in rows)


def list_event_members(conn: Connection, event_id: str) -> tuple[EventMember, ...]:
    get_event(conn, event_id)
    rows = conn.execute(
        "SELECT file_id, role, added_at FROM event_members WHERE event_id=? ORDER BY file_id", (event_id,)
    ).fetchall()
    return tuple(EventMember(r["file_id"], r["role"], r["added_at"]) for r in rows)


def list_event_history(conn: Connection, event_id: str) -> tuple[EventHistoryEntry, ...]:
    get_event(conn, event_id)
    rows = conn.execute("SELECT * FROM event_history WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
    return tuple(EventHistoryEntry(r["id"], r["event_id"], r["action"], r["file_id"],
                                   r["member_role"], r["old_value"], r["new_value"], r["created_at"])
                 for r in rows)


def rename_event(conn: Connection, event_id: str, name: str) -> Event:
    name = _clean_name(name); event = get_event(conn, event_id)
    if name == event.name:
        return event
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE events SET name=?, updated_at=? WHERE id=?", (name, now, event_id))
        conn.execute("INSERT INTO event_history(event_id,action,old_value,new_value,created_at) VALUES (?,'rename',?,?,?)",
                     (event_id, event.name, name, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event(conn, event_id)


def update_event_note(conn: Connection, event_id: str, note: str | None) -> Event:
    note = _clean_note(note); event = get_event(conn, event_id)
    if note == event.note:
        return event
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE events SET note=?, updated_at=? WHERE id=?", (note, now, event_id))
        conn.execute("INSERT INTO event_history(event_id,action,old_value,new_value,created_at) VALUES (?,'note',?,?,?)",
                     (event_id, event.note, note, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event(conn, event_id)


def add_event_member(conn: Connection, event_id: str, file_id: str) -> Event:
    event = get_event(conn, event_id)
    row = conn.execute("SELECT library_id FROM files WHERE id=?", (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"file not found: {file_id}")
    if row["library_id"] != event.library_id:
        raise ValueError("event member library mismatch")
    if conn.execute("SELECT 1 FROM event_members WHERE event_id=? AND file_id=?", (event_id, file_id)).fetchone():
        return event
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO event_members(event_id,file_id,role,added_at) VALUES (?,?,'human_added',?)",
                     (event_id, file_id, now))
        conn.execute("UPDATE events SET updated_at=? WHERE id=?", (now, event_id))
        conn.execute("INSERT INTO event_history(event_id,action,file_id,member_role,new_value,created_at) "
                     "VALUES (?,'add_member',?,'human_added','member',?)", (event_id, file_id, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event(conn, event_id)


def remove_event_member(conn: Connection, event_id: str, file_id: str) -> Event:
    event = get_event(conn, event_id)
    row = conn.execute("SELECT role FROM event_members WHERE event_id=? AND file_id=?", (event_id, file_id)).fetchone()
    if row is None:
        raise ValueError("photo is not a member of this event")
    if len(event.file_ids) <= 1:
        raise ValueError("an event must retain at least one member")
    role = row["role"]; now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM event_members WHERE event_id=? AND file_id=?", (event_id, file_id))
        conn.execute("UPDATE events SET updated_at=? WHERE id=?", (now, event_id))
        conn.execute("INSERT INTO event_history(event_id,action,file_id,member_role,old_value,created_at) "
                     "VALUES (?,'remove_member',?,?, 'member',?)", (event_id, file_id, role, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event(conn, event_id)


def event_for_cluster(conn: Connection, *, library_id: int, cluster_key: str) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE library_id=? AND source_cluster_key=?", (library_id, cluster_key)).fetchone()
    return _row_to_event(conn, row) if row is not None else None


def items_for_event(view: TimelineView, event: Event, *, lane: str | None = None) -> tuple[TimelineItem, ...]:
    valid = {"placed", "range", "tentative", "unplaced"}
    if lane is not None and lane not in valid:
        raise ValueError(f"unknown timeline lane: {lane!r}")
    members = set(event.file_ids)
    return tuple(i for i in view.items if i.file_id in members and (lane is None or i.lane == lane))

@dataclass(frozen=True)
class EventContext:
    event_id: str
    description: str | None
    place_text: str | None
    people_text: str | None
    occasion_text: str | None
    story_text: str | None
    updated_at: str | None


@dataclass(frozen=True)
class EventContextHistoryEntry:
    id: int
    event_id: str
    old_value: str | None
    new_value: str
    created_at: str


def _clean_context_value(value: str | None, *, limit: int, label: str) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise ValueError(f"event {label} is too long")
    return cleaned


def get_event_context(conn: Connection, event_id: str) -> EventContext:
    get_event(conn, event_id)
    row = conn.execute("SELECT * FROM event_context WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        return EventContext(event_id, None, None, None, None, None, None)
    return EventContext(row["event_id"], row["description"], row["place_text"], row["people_text"],
                        row["occasion_text"], row["story_text"], row["updated_at"])


def _context_json(context: EventContext) -> str:
    return json.dumps({
        "description": context.description,
        "place_text": context.place_text,
        "people_text": context.people_text,
        "occasion_text": context.occasion_text,
        "story_text": context.story_text,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def update_event_context(conn: Connection, event_id: str, *, description: str | None = None,
                         place_text: str | None = None, people_text: str | None = None,
                         occasion_text: str | None = None, story_text: str | None = None) -> EventContext:
    """Replace the current human narrative context and append an audit snapshot.

    These fields are descriptive human memory only. They are never consumed by
    chronology/reconstruction code.
    """
    old = get_event_context(conn, event_id)
    new = EventContext(
        event_id,
        _clean_context_value(description, limit=8000, label="description"),
        _clean_context_value(place_text, limit=500, label="place"),
        _clean_context_value(people_text, limit=2000, label="people"),
        _clean_context_value(occasion_text, limit=500, label="occasion"),
        _clean_context_value(story_text, limit=12000, label="story"),
        _now(),
    )
    if _context_json(old) == _context_json(new):
        return old
    old_json = _context_json(old) if old.updated_at is not None else None
    new_json = _context_json(new)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO event_context(event_id,description,place_text,people_text,occasion_text,story_text,updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
            "description=excluded.description,place_text=excluded.place_text,people_text=excluded.people_text,"
            "occasion_text=excluded.occasion_text,story_text=excluded.story_text,updated_at=excluded.updated_at",
            (event_id, new.description, new.place_text, new.people_text, new.occasion_text, new.story_text, new.updated_at),
        )
        conn.execute("INSERT INTO event_context_history(event_id,old_value,new_value,created_at) VALUES (?,?,?,?)",
                     (event_id, old_json, new_json, new.updated_at))
        conn.execute("UPDATE events SET updated_at=? WHERE id=?", (new.updated_at, event_id))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event_context(conn, event_id)


def list_event_context_history(conn: Connection, event_id: str) -> tuple[EventContextHistoryEntry, ...]:
    get_event(conn, event_id)
    rows = conn.execute("SELECT * FROM event_context_history WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
    return tuple(EventContextHistoryEntry(r["id"], r["event_id"], r["old_value"], r["new_value"], r["created_at"])
                 for r in rows)

# ---------------------------------------------------------------------------
# Phase 8.10 — presentation preferences (display-only human choices)

@dataclass(frozen=True)
class EventPresentation:
    event_id: str
    cover_file_id: str | None
    order_file_ids: tuple[str, ...] | None
    updated_at: str | None


@dataclass(frozen=True)
class EventPresentationHistoryEntry:
    id: int
    event_id: str
    action: str
    old_value: str | None
    new_value: str | None
    created_at: str


def get_event_presentation(conn: Connection, event_id: str) -> EventPresentation:
    event = get_event(conn, event_id)
    row = conn.execute("SELECT * FROM event_presentation WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        return EventPresentation(event_id, None, None, None)
    order = None
    if row["order_json"]:
        try:
            parsed = json.loads(row["order_json"])
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                values = tuple(parsed)
                # Old/stale/corrupt presentation order is ignored unless it is
                # an exact permutation of current membership.
                if len(values) == len(event.file_ids) and set(values) == set(event.file_ids):
                    order = values
        except (TypeError, ValueError, json.JSONDecodeError):
            order = None
    cover = row["cover_file_id"] if row["cover_file_id"] in set(event.file_ids) else None
    return EventPresentation(event_id, cover, order, row["updated_at"])


def _presentation_json(p: EventPresentation) -> str:
    return json.dumps({
        "cover_file_id": p.cover_file_id,
        "order_file_ids": list(p.order_file_ids) if p.order_file_ids is not None else None,
    }, sort_keys=True, separators=(",", ":"))


def set_event_cover(conn: Connection, event_id: str, file_id: str | None) -> EventPresentation:
    event = get_event(conn, event_id)
    if file_id is not None and file_id not in set(event.file_ids):
        raise ValueError("preferred cover must be a current event member")
    old = get_event_presentation(conn, event_id)
    if old.cover_file_id == file_id:
        return old
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO event_presentation(event_id,cover_file_id,order_json,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET cover_file_id=excluded.cover_file_id, updated_at=excluded.updated_at",
            (event_id, file_id, json.dumps(list(old.order_file_ids)) if old.order_file_ids is not None else None, now),
        )
        new = EventPresentation(event_id, file_id, old.order_file_ids, now)
        conn.execute(
            "INSERT INTO event_presentation_history(event_id,action,old_value,new_value,created_at) "
            "VALUES (?,'cover',?,?,?)",
            (event_id, _presentation_json(old), _presentation_json(new), now),
        )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event_presentation(conn, event_id)


def set_event_presentation_order(conn: Connection, event_id: str,
                                 file_ids: tuple[str, ...] | list[str]) -> EventPresentation:
    event = get_event(conn, event_id)
    values = tuple(str(x) for x in file_ids)
    if len(values) != len(set(values)):
        raise ValueError("presentation order contains duplicate members")
    if len(values) != len(event.file_ids) or set(values) != set(event.file_ids):
        raise ValueError("presentation order must contain every current event member exactly once")
    old = get_event_presentation(conn, event_id)
    if old.order_file_ids == values:
        return old
    now = _now(); order_json = json.dumps(list(values), separators=(",", ":"))
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO event_presentation(event_id,cover_file_id,order_json,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET order_json=excluded.order_json, updated_at=excluded.updated_at",
            (event_id, old.cover_file_id, order_json, now),
        )
        new = EventPresentation(event_id, old.cover_file_id, values, now)
        conn.execute(
            "INSERT INTO event_presentation_history(event_id,action,old_value,new_value,created_at) "
            "VALUES (?,'order',?,?,?)",
            (event_id, _presentation_json(old), _presentation_json(new), now),
        )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event_presentation(conn, event_id)


def reset_event_presentation(conn: Connection, event_id: str) -> EventPresentation:
    get_event(conn, event_id)
    old = get_event_presentation(conn, event_id)
    if old.cover_file_id is None and old.order_file_ids is None:
        return old
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO event_presentation(event_id,cover_file_id,order_json,updated_at) VALUES (?,NULL,NULL,?) "
            "ON CONFLICT(event_id) DO UPDATE SET cover_file_id=NULL, order_json=NULL, updated_at=excluded.updated_at",
            (event_id, now),
        )
        new = EventPresentation(event_id, None, None, now)
        conn.execute(
            "INSERT INTO event_presentation_history(event_id,action,old_value,new_value,created_at) "
            "VALUES (?,'reset',?,?,?)",
            (event_id, _presentation_json(old), _presentation_json(new), now),
        )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return get_event_presentation(conn, event_id)


def list_event_presentation_history(conn: Connection, event_id: str) -> tuple[EventPresentationHistoryEntry, ...]:
    get_event(conn, event_id)
    rows = conn.execute(
        "SELECT * FROM event_presentation_history WHERE event_id=? ORDER BY id", (event_id,)
    ).fetchall()
    return tuple(EventPresentationHistoryEntry(
        r["id"], r["event_id"], r["action"], r["old_value"], r["new_value"], r["created_at"]
    ) for r in rows)
