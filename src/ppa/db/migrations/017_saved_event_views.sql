-- Phase 8.12 — durable saved Event discovery views.
-- Saved views are presentation/search presets only. They store no Event result
-- membership and therefore cannot become chronology or Event authority.

CREATE TABLE saved_event_views (
    id              TEXT PRIMARY KEY,
    library_id      INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    query_text      TEXT NOT NULL DEFAULT '',
    year            INTEGER,
    start_date      TEXT,
    end_date        TEXT,
    occasion_filter TEXT,
    place_filter    TEXT,
    person_filter   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(library_id, name COLLATE NOCASE)
);
CREATE INDEX idx_saved_event_views_library_name
    ON saved_event_views(library_id, name COLLATE NOCASE, id);
