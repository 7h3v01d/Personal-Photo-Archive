-- Phase 10.3 — controlled logical-Photo identity resolution audit.
-- A split reassigns physical File records only; source bytes and FileRevision
-- evidence remain untouched. History is append-only and records the exact
-- cohort moved into the newly-created logical Photo.

CREATE TABLE identity_resolution_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution_id       TEXT NOT NULL,
    action              TEXT NOT NULL CHECK(action = 'split_hash_cohort'),
    library_id           INTEGER NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
    source_photo_id      TEXT NOT NULL,
    new_photo_id         TEXT NOT NULL,
    sha256               TEXT NOT NULL,
    file_ids_json        TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    note                 TEXT,
    created_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_identity_resolution_id ON identity_resolution_history(resolution_id);
CREATE INDEX idx_identity_resolution_source ON identity_resolution_history(source_photo_id, id);
CREATE INDEX idx_identity_resolution_new ON identity_resolution_history(new_photo_id, id);
