-- Personal Photo Archive — Migration 004: Provenance ownership invariants
--
-- The foreign keys prove a referenced revision EXISTS, but not that it BELONGS
-- to the right file. For a long-term archive whose provenance will be treated
-- as evidence, the database itself should reject cross-owner links:
--
--   * a File may only point its current_revision_id at one of ITS OWN revisions
--   * a MetadataObservation's file_id must match its revision's file_id
--
-- Enforced with triggers (SQLite has no CHECK across tables). No transaction
-- control here — the migration runner wraps this file.

CREATE TRIGGER IF NOT EXISTS trg_files_current_revision_owner_ins
BEFORE INSERT ON files
FOR EACH ROW WHEN NEW.current_revision_id IS NOT NULL
  AND (SELECT file_id FROM file_revisions WHERE id = NEW.current_revision_id) IS NOT NEW.id
BEGIN
    SELECT RAISE(ABORT, 'current_revision_id must belong to this file');
END;

CREATE TRIGGER IF NOT EXISTS trg_files_current_revision_owner_upd
BEFORE UPDATE OF current_revision_id ON files
FOR EACH ROW WHEN NEW.current_revision_id IS NOT NULL
  AND (SELECT file_id FROM file_revisions WHERE id = NEW.current_revision_id) IS NOT NEW.id
BEGIN
    SELECT RAISE(ABORT, 'current_revision_id must belong to this file');
END;

CREATE TRIGGER IF NOT EXISTS trg_observations_revision_owner_ins
BEFORE INSERT ON metadata_observations
FOR EACH ROW WHEN NEW.file_revision_id IS NOT NULL
  AND (SELECT file_id FROM file_revisions WHERE id = NEW.file_revision_id) IS NOT NEW.file_id
BEGIN
    SELECT RAISE(ABORT, 'observation file_id must match its revision''s file');
END;

CREATE TRIGGER IF NOT EXISTS trg_observations_revision_owner_upd
BEFORE UPDATE ON metadata_observations
FOR EACH ROW WHEN NEW.file_revision_id IS NOT NULL
  AND (SELECT file_id FROM file_revisions WHERE id = NEW.file_revision_id) IS NOT NEW.file_id
BEGIN
    SELECT RAISE(ABORT, 'observation file_id must match its revision''s file');
END;
