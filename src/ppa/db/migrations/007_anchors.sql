-- Anchors: user-asserted calendar evidence about when photos were taken.
-- These are INTERPRETATION, not observation: a human states a known/approximate
-- date for a file, a directory, or a whole library. They are stored entirely
-- separately from observations and never touch a photo's bytes or metadata.
-- Dates are day-granularity ISO strings (YYYY-MM-DD); "Christmas 2004" does not
-- claim a specific instant.

CREATE TABLE IF NOT EXISTS anchors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL CHECK (scope IN ('file', 'directory', 'library')),
    scope_ref   TEXT NOT NULL,           -- file_id / directory path / library_id
    kind        TEXT NOT NULL CHECK (kind IN ('exact', 'range')),
    start_date  TEXT NOT NULL,           -- YYYY-MM-DD
    end_date    TEXT,                    -- YYYY-MM-DD, range only
    note        TEXT,
    created_at  TEXT NOT NULL,
    CHECK ((kind = 'exact' AND end_date IS NULL)
        OR (kind = 'range' AND end_date IS NOT NULL AND end_date >= start_date))
);

CREATE INDEX IF NOT EXISTS idx_anchors_scope ON anchors (scope, scope_ref);
