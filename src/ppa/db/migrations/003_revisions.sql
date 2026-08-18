-- Personal Photo Archive — Migration 003: Revisions, presence/health, scan state
--
-- The heart of archive-core hardening. A File now owns an immutable ledger of
-- FileRevisions. A content change appends a new revision and moves the File's
-- current_revision_id pointer; the old revision row is NEVER mutated or
-- deleted, so the archive keeps the full history of "what those bytes were and
-- what metadata they claimed" (essential for later date reconstruction).
--
-- Also decomposes the overloaded `status` into presence (is the file there?)
-- and health (is it readable / does it still hash the same?), and gives each
-- scan an explicit completion state so an INCOMPLETE traversal can never be
-- allowed to mark files missing.
--
-- No transaction control here — the migration runner wraps this file.

CREATE TABLE IF NOT EXISTS file_revisions (
    id                 TEXT PRIMARY KEY,
    file_id            TEXT NOT NULL REFERENCES files(id),
    sha256             TEXT,                         -- full content hash (immutable once set)
    size_bytes         INTEGER,
    width_px           INTEGER,
    height_px          INTEGER,
    fs_mtime           TEXT,
    first_observed_at  TEXT NOT NULL,
    observed_session   TEXT,
    superseded_at      TEXT,                         -- NULL while current
    -- extraction lifecycle for THIS revision's content:
    --   pending | success | failed_transient | failed_unreadable | unsupported
    extraction_status  TEXT NOT NULL DEFAULT 'pending',
    extracted_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_revisions_file ON file_revisions(file_id);
CREATE INDEX IF NOT EXISTS idx_revisions_sha  ON file_revisions(sha256);

-- Files point at their current revision; presence and health are separate axes.
ALTER TABLE files ADD COLUMN current_revision_id TEXT REFERENCES file_revisions(id);
ALTER TABLE files ADD COLUMN presence_status TEXT NOT NULL DEFAULT 'present'; -- present|missing|unknown
ALTER TABLE files ADD COLUMN health_status   TEXT NOT NULL DEFAULT 'ok';      -- ok|unreadable|hash_mismatch|unknown

-- Observations belong to the revision whose content they describe.
ALTER TABLE metadata_observations ADD COLUMN file_revision_id TEXT REFERENCES file_revisions(id);

-- Scan completeness — the fail-closed invariant depends on this.
ALTER TABLE import_sessions ADD COLUMN scan_status TEXT NOT NULL DEFAULT 'complete'; -- running|complete|incomplete|failed
ALTER TABLE import_sessions ADD COLUMN traversal_errors INTEGER NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- Backfill: give every existing file one revision from its current content,
-- point the file at it, carry presence across from status, and attach existing
-- observations to that revision.
-- ---------------------------------------------------------------------------

INSERT INTO file_revisions (
    id, file_id, sha256, size_bytes, width_px, height_px, fs_mtime,
    first_observed_at, extraction_status
)
SELECT
    lower(hex(randomblob(16))), id, sha256, size_bytes, width_px, height_px,
    fs_mtime, first_seen_at,
    CASE WHEN sha256 IS NOT NULL AND EXISTS (
        SELECT 1 FROM metadata_observations o
        WHERE o.file_id = files.id AND o.source = 'meta'
          AND o.key = '_extracted_from_sha' AND o.value = files.sha256
    ) THEN 'success' ELSE 'pending' END
FROM files;

UPDATE files SET current_revision_id = (
    SELECT r.id FROM file_revisions r WHERE r.file_id = files.id LIMIT 1
);

UPDATE files SET presence_status = CASE status WHEN 'missing' THEN 'missing' ELSE 'present' END;

UPDATE metadata_observations SET file_revision_id = (
    SELECT r.id FROM file_revisions r WHERE r.file_id = metadata_observations.file_id LIMIT 1
) WHERE file_revision_id IS NULL;
