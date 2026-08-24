-- Phase 8.10 — human event presentation preferences.
-- These preferences affect display only. They never alter event membership,
-- chronology evidence, reconstructions, metadata, or source photographs.

CREATE TABLE event_presentation (
    event_id       TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    cover_file_id  TEXT REFERENCES files(id) ON DELETE SET NULL,
    order_json     TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE event_presentation_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('cover','order','reset')),
    old_value   TEXT,
    new_value   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_event_presentation_history_event
    ON event_presentation_history(event_id, id);

-- A preferred cover must be a current member of its Event.
CREATE TRIGGER event_presentation_cover_guard_insert
BEFORE INSERT ON event_presentation
WHEN NEW.cover_file_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM event_members m
        WHERE m.event_id = NEW.event_id AND m.file_id = NEW.cover_file_id
    ) THEN RAISE(ABORT, 'event cover must be a current event member') END;
END;

CREATE TRIGGER event_presentation_cover_guard_update
BEFORE UPDATE OF cover_file_id ON event_presentation
WHEN NEW.cover_file_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM event_members m
        WHERE m.event_id = NEW.event_id AND m.file_id = NEW.cover_file_id
    ) THEN RAISE(ABORT, 'event cover must be a current event member') END;
END;

-- Membership changes invalidate presentation choices that depended on the old
-- exact set. The append-only presentation history remains intact.
CREATE TRIGGER event_member_presentation_cleanup_delete
AFTER DELETE ON event_members
BEGIN
    UPDATE event_presentation
       SET cover_file_id = CASE WHEN cover_file_id = OLD.file_id THEN NULL ELSE cover_file_id END,
           order_json = NULL,
           updated_at = datetime('now')
     WHERE event_id = OLD.event_id;
END;

CREATE TRIGGER event_member_presentation_cleanup_insert
AFTER INSERT ON event_members
BEGIN
    UPDATE event_presentation
       SET order_json = NULL,
           updated_at = datetime('now')
     WHERE event_id = NEW.event_id;
END;
