-- Phase 8.4 — durable human-authored event identity.
--
-- Provisional timeline clusters are derived browsing context and may change as
-- chronology improves. A human-named event is interpretation and therefore gets
-- a stable UUID plus an explicit membership snapshot. It never alters photo
-- metadata or date evidence.

CREATE TABLE events (
    id                  TEXT PRIMARY KEY,
    library_id          INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    note                TEXT,
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    source_kind         TEXT NOT NULL DEFAULT 'timeline_cluster'
                        CHECK (source_kind IN ('timeline_cluster', 'manual')),
    source_cluster_key  TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    CHECK (length(trim(name)) > 0),
    CHECK (end_date >= start_date)
);

CREATE UNIQUE INDEX idx_events_library_cluster
    ON events(library_id, source_cluster_key)
    WHERE source_cluster_key IS NOT NULL;
CREATE INDEX idx_events_library_dates ON events(library_id, start_date, end_date);

CREATE TABLE event_members (
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    file_id     TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'authoritative_seed'
                CHECK (role IN ('authoritative_seed', 'human_added')),
    added_at    TEXT NOT NULL,
    PRIMARY KEY (event_id, file_id)
);
CREATE INDEX idx_event_members_file ON event_members(file_id);

-- Event ownership is library-scoped. Reject cross-library membership even if a
-- caller bypasses the Python API.
CREATE TRIGGER event_member_library_guard_insert
BEFORE INSERT ON event_members
BEGIN
    SELECT CASE WHEN (
        SELECT e.library_id FROM events e WHERE e.id = NEW.event_id
    ) IS NULL OR (
        SELECT f.library_id FROM files f WHERE f.id = NEW.file_id
    ) IS NULL OR (
        SELECT e.library_id FROM events e WHERE e.id = NEW.event_id
    ) != (
        SELECT f.library_id FROM files f WHERE f.id = NEW.file_id
    ) THEN RAISE(ABORT, 'event member library mismatch') END;
END;
