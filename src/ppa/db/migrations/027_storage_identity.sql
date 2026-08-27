-- Personal Photo Archive — Migration 027: Filesystem storage identity
--
-- Phase 12.1 records the filesystem object identity observed during normal
-- source-library scans.  This closes the hard-link ambiguity in Archive
-- Health without turning that read model into a source-file operation.
--
-- The identifiers are deliberately TEXT.  Platform stat identifiers can be
-- unsigned / wider than SQLite's signed INTEGER range, and PPA compares them
-- only as opaque current-observation tokens.  NULL means "not established on
-- this platform/scan"; migration never invents a value for existing Files.
--
-- No transaction control here — the migration runner wraps this file.

ALTER TABLE files ADD COLUMN fs_device_id TEXT;
ALTER TABLE files ADD COLUMN fs_object_id TEXT;
ALTER TABLE files ADD COLUMN fs_link_count INTEGER;
ALTER TABLE files ADD COLUMN fs_identity_observed_at TEXT;
ALTER TABLE files ADD COLUMN fs_identity_session TEXT REFERENCES import_sessions(id);

CREATE INDEX IF NOT EXISTS idx_files_fs_object
    ON files(fs_device_id, fs_object_id);

-- Sparse history: one row when identity is established and another
-- only when the object identity or link count changes.  Routine rescans update
-- the current observation timestamp on files without appending unbounded
-- duplicate history rows.
CREATE TABLE file_storage_identity_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    device_id   TEXT,
    object_id   TEXT,
    link_count  INTEGER,
    path        TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    session_id  TEXT REFERENCES import_sessions(id),
    reason      TEXT NOT NULL CHECK (
        reason IN ('identity_established', 'identity_changed', 'identity_unavailable', 'link_count_changed')
    )
);

CREATE INDEX IF NOT EXISTS idx_storage_identity_history_file
    ON file_storage_identity_history(file_id, id);
CREATE INDEX IF NOT EXISTS idx_storage_identity_history_object
    ON file_storage_identity_history(device_id, object_id);
