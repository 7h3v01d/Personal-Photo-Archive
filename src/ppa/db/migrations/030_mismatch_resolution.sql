-- Personal Photo Archive — Migration 030: Controlled hash-mismatch resolution
--
-- Human disposition is kept separate from machine health.  A mismatch remains
-- health_status='hash_mismatch' when the user retains the expected revision or
-- records an unresolved review.  Only the explicit adopt-current action moves
-- File.current_revision_id to a newly appended immutable FileRevision.
-- Source photographs are never modified by this table or its resolution code.

CREATE TABLE IF NOT EXISTS integrity_mismatch_resolutions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution_id           TEXT NOT NULL UNIQUE,
    file_id                 TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    action                  TEXT NOT NULL CHECK(action IN (
                                'retain_expected_recovery_needed',
                                'adopt_current_revision',
                                'reviewed_unresolved'
                            )),
    expected_revision_id    TEXT NOT NULL REFERENCES file_revisions(id) ON DELETE CASCADE,
    expected_sha256         TEXT NOT NULL,
    reviewed_observation_id INTEGER REFERENCES integrity_mismatch_observations(id) ON DELETE SET NULL,
    reviewed_current_state  TEXT NOT NULL,
    reviewed_current_sha256 TEXT,
    observed_path           TEXT NOT NULL,
    observed_size_bytes     INTEGER,
    observed_mtime_ns       INTEGER,
    adopted_revision_id     TEXT REFERENCES file_revisions(id) ON DELETE CASCADE,
    adopted_sha256          TEXT,
    evidence_fingerprint    TEXT NOT NULL,
    note                    TEXT,
    resolved_at             TEXT NOT NULL,
    CHECK (
        (action='adopt_current_revision' AND adopted_revision_id IS NOT NULL AND adopted_sha256 IS NOT NULL)
        OR
        (action<>'adopt_current_revision' AND adopted_revision_id IS NULL AND adopted_sha256 IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_mismatch_resolutions_file
    ON integrity_mismatch_resolutions(file_id, resolved_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_mismatch_resolutions_expected_revision
    ON integrity_mismatch_resolutions(expected_revision_id);
