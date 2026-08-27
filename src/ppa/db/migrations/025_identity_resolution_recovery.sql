-- Phase 10.4 — append-only recovery audit for controlled identity splits.
CREATE TABLE identity_resolution_recovery_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    recovery_id           TEXT NOT NULL UNIQUE,
    resolution_id         TEXT NOT NULL,
    action                TEXT NOT NULL CHECK(action='recombine_split'),
    library_id            INTEGER NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
    source_photo_id        TEXT NOT NULL,
    recombined_photo_id    TEXT NOT NULL,
    file_ids_json          TEXT NOT NULL,
    evidence_fingerprint   TEXT NOT NULL,
    note                   TEXT,
    created_at             TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_identity_resolution_recovery_resolution
    ON identity_resolution_recovery_history(resolution_id);
CREATE INDEX idx_identity_resolution_recovery_photo
    ON identity_resolution_recovery_history(source_photo_id, recombined_photo_id, id);
