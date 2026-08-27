-- Phase 10.7 — append-only audit for controlled competing-identity merges.
CREATE TABLE identity_merge_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_id              TEXT NOT NULL UNIQUE,
    action                TEXT NOT NULL CHECK(action='merge_competing_identity'),
    library_id            INTEGER NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
    sha256                TEXT NOT NULL,
    survivor_photo_id     TEXT NOT NULL,
    retired_photo_id      TEXT NOT NULL,
    moved_file_ids_json   TEXT NOT NULL,
    evidence_fingerprint  TEXT NOT NULL,
    note                  TEXT,
    created_at             TEXT NOT NULL
);
CREATE INDEX idx_identity_merge_survivor ON identity_merge_history(survivor_photo_id, id);
CREATE INDEX idx_identity_merge_retired ON identity_merge_history(retired_photo_id, id);
