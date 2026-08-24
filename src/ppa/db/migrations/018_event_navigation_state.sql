-- Phase 8.13 — lightweight personal Event navigation state.
-- Favourites and recent views are presentation preferences only.

CREATE TABLE event_navigation_state (
    event_id        TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    library_id      INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    favorite        INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0,1)),
    last_viewed_at  TEXT,
    view_count      INTEGER NOT NULL DEFAULT 0 CHECK (view_count >= 0),
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_event_navigation_recent
    ON event_navigation_state(library_id, last_viewed_at DESC, event_id);
CREATE INDEX idx_event_navigation_favorite
    ON event_navigation_state(library_id, favorite, event_id);

CREATE TRIGGER event_navigation_library_guard_insert
BEFORE INSERT ON event_navigation_state
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM events e
        WHERE e.id = NEW.event_id AND e.library_id = NEW.library_id
    ) THEN RAISE(ABORT, 'event navigation library mismatch') END;
END;

CREATE TRIGGER event_navigation_library_guard_update
BEFORE UPDATE OF event_id, library_id ON event_navigation_state
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM events e
        WHERE e.id = NEW.event_id AND e.library_id = NEW.library_id
    ) THEN RAISE(ABORT, 'event navigation library mismatch') END;
END;
