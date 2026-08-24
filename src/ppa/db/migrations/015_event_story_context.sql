-- Phase 8.6 — human-authored event story/context.
-- Narrative memory is interpretation, never chronology evidence.

CREATE TABLE event_context (
    event_id       TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    description    TEXT,
    place_text     TEXT,
    people_text    TEXT,
    occasion_text  TEXT,
    story_text     TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE event_context_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_event_context_history_event ON event_context_history(event_id, id);
