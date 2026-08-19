-- Personal Photo Archive — Migration 005: Within-library identity + extractor provenance
--
-- Two closures:
--   * A File's identity within its Library should be its canonical relative
--     path, not the raw absolute-path string (which changes when the same
--     library root is opened with a different spelling / case / symlink).
--     relative_path_key = normcase(normpath(relative_path)).
--   * Metadata provenance (which extractor produced a revision's observations)
--     lives on the revision, so a newer extractor version can mark old
--     extractions stale — replacing the earlier, now-unused marker rows.
--
-- No transaction control here — the migration runner wraps this file.

ALTER TABLE files ADD COLUMN relative_path_key TEXT;
CREATE INDEX IF NOT EXISTS idx_files_relkey ON files(library_id, relative_path_key);

ALTER TABLE file_revisions ADD COLUMN extractor_name TEXT;
ALTER TABLE file_revisions ADD COLUMN extractor_version TEXT;
