-- Phase 9.7 — durable saved organisation discovery recipes.
-- Stores only Album/Tag selector identity, never cached Photo result membership.

CREATE TABLE saved_organization_views (
    id          TEXT PRIMARY KEY,
    library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    album_ids_json TEXT NOT NULL DEFAULT '[]',
    tag_ids_json   TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(library_id, name COLLATE NOCASE)
);
CREATE INDEX idx_saved_organization_views_library_name
    ON saved_organization_views(library_id, name COLLATE NOCASE, id);
