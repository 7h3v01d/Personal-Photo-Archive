-- Reconstruction provenance binding (schema v11).
--
-- A confirmed reconstruction is a human decision about a SPECIFIC set of bytes.
-- Previously reconstructions were keyed only by file_id, so a confirmation
-- survived a change of the file's current revision (new bytes at the same File
-- identity) — authority crossing a revision boundary. We now record the revision
-- the reconstruction was derived against, so a decision can be recognised as
-- STALE (no longer authoritative) once the current revision changes, without
-- deleting the historical decision.
--
-- Also split time semantics: created_at is the row's birth; updated_at is the
-- last recompute. And record the engine version for reproducibility.

ALTER TABLE reconstructions ADD COLUMN source_revision_id TEXT REFERENCES file_revisions(id);
ALTER TABLE reconstructions ADD COLUMN engine_version TEXT;
ALTER TABLE reconstructions ADD COLUMN updated_at TEXT;

UPDATE reconstructions SET updated_at = created_at WHERE updated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_reconstructions_revision ON reconstructions (source_revision_id);
