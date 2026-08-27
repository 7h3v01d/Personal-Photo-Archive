-- Personal Photo Archive — Migration 023: explicit Photo lineage
--
-- Exact duplicate bytes are NOT lineage edges: byte-identical Files already
-- share one logical Photo identity. This table connects distinct logical Photos
-- only when a human explicitly records a derivative/variant relationship.

CREATE TABLE photo_lineage (
    id               TEXT PRIMARY KEY,
    parent_photo_id  TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    child_photo_id   TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    relation_type    TEXT NOT NULL CHECK (relation_type IN (
        'derived_copy','edited_variant','resized_variant','format_conversion',
        'crop','unknown_derivative'
    )),
    source           TEXT NOT NULL DEFAULT 'human' CHECK (source = 'human'),
    note             TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(parent_photo_id, child_photo_id),
    CHECK(parent_photo_id <> child_photo_id)
);

CREATE INDEX idx_photo_lineage_parent ON photo_lineage(parent_photo_id);
CREATE INDEX idx_photo_lineage_child ON photo_lineage(child_photo_id);

CREATE TABLE photo_lineage_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lineage_id       TEXT NOT NULL,
    action           TEXT NOT NULL CHECK (action IN ('create','remove')),
    parent_photo_id  TEXT NOT NULL,
    child_photo_id   TEXT NOT NULL,
    relation_type    TEXT NOT NULL,
    source           TEXT NOT NULL,
    note             TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_photo_lineage_history_lineage ON photo_lineage_history(lineage_id, id);

-- Raw SQL must not be able to create cycles even if it bypasses the Python API.
CREATE TRIGGER photo_lineage_no_cycle
BEFORE INSERT ON photo_lineage
BEGIN
    SELECT CASE WHEN EXISTS (
        WITH RECURSIVE descendants(photo_id) AS (
            SELECT child_photo_id FROM photo_lineage
             WHERE parent_photo_id = NEW.child_photo_id
            UNION
            SELECT pl.child_photo_id
              FROM photo_lineage pl
              JOIN descendants d ON pl.parent_photo_id = d.photo_id
        )
        SELECT 1 FROM descendants WHERE photo_id = NEW.parent_photo_id
    ) THEN RAISE(ABORT, 'photo lineage cycle') END;
END;
