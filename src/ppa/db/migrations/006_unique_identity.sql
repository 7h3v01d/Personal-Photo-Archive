-- Personal Photo Archive — Migration 006: Unique within-library identity
--
-- The declared identity of a File is (library_id, relative_path_key). Enforce
-- it in the database so one physical path within one library can only be one
-- File.
--
-- Scoped to PRESENT files: a missing File keeps its relative_path_key as
-- history, and a genuinely different photo may later be created at that same
-- relative path. Only one *present* File may hold a given identity at a time,
-- which is the real filesystem invariant.
--
-- No transaction control here — the migration runner wraps this file.

CREATE UNIQUE INDEX IF NOT EXISTS uq_files_present_identity
ON files(library_id, relative_path_key)
WHERE library_id IS NOT NULL
  AND relative_path_key IS NOT NULL
  AND presence_status = 'present';
