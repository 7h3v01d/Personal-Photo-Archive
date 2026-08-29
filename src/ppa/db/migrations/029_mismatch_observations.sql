-- Personal Photo Archive — Migration 029: Structured hash-mismatch observations
--
-- integrity_events remains the human-readable append-only forensic ledger.  This
-- companion table retains the machine-readable hashes observed by Verify so a
-- later investigation never has to parse prose to recover what was actually
-- seen.  Expected identity is bound to the immutable FileRevision that was
-- current at verification time.  No source-file content is stored here.

CREATE TABLE IF NOT EXISTS integrity_mismatch_observations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id               TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    expected_revision_id  TEXT NOT NULL REFERENCES file_revisions(id) ON DELETE CASCADE,
    expected_sha256       TEXT NOT NULL,
    observed_sha256       TEXT NOT NULL,
    observed_path         TEXT NOT NULL,
    observed_size_bytes   INTEGER,
    observed_mtime_ns     INTEGER,
    observed_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_mismatch_observations_file
    ON integrity_mismatch_observations(file_id, observed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_mismatch_observations_revision
    ON integrity_mismatch_observations(expected_revision_id);
