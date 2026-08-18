-- Personal Photo Archive — Migration 002: Libraries
--
-- Introduces the Library as a first-class entity and scopes every File to
-- the library it was found in. This closes the multi-library reconciliation
-- blockers: a scan of Library B must never conclude that Library A's files
-- have moved or gone missing, because it was never looking at Library A.
--
-- A File is identified within its library by a relative_path (relative to
-- the library root), while the absolute `path` column is retained as a
-- maintained convenience for the read model and thumbnailing. Storing the
-- relative path is what will later let an entire archive root move from
-- D:\Family Photos to E:\Photos without reinterpreting 10,000 files as moves.
--
-- No transaction control here — the migration runner wraps this file.

CREATE TABLE IF NOT EXISTS libraries (
    id                   INTEGER PRIMARY KEY,
    root_display_path    TEXT NOT NULL,          -- exactly as the user chose it
    root_canonical_path  TEXT NOT NULL UNIQUE,   -- normalised, for comparison
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_scan_at         TEXT,
    state                TEXT NOT NULL DEFAULT 'active'
);

-- Scope columns on files. Nullable so existing (pre-libraries) rows migrate
-- cleanly; the scanner adopts any legacy row it finds under a library root.
ALTER TABLE files ADD COLUMN library_id INTEGER REFERENCES libraries(id);
ALTER TABLE files ADD COLUMN relative_path TEXT;

CREATE INDEX IF NOT EXISTS idx_files_library ON files(library_id);
