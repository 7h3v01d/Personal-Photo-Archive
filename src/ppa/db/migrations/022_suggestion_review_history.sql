-- Phase 9.10 — durable review state for ephemeral organisation-suggestion fingerprints.
-- Review state is navigation/curation workflow metadata only. It never grants
-- Album/Tag membership or chronology authority.

CREATE TABLE organization_suggestion_reviews (
    library_id      INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    suggestion_id   TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('dismissed','accepted')),
    note            TEXT,
    reviewed_at     TEXT NOT NULL,
    PRIMARY KEY(library_id, suggestion_id)
);

CREATE TABLE organization_suggestion_review_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id      INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    suggestion_id   TEXT NOT NULL,
    action          TEXT NOT NULL CHECK(action IN ('dismiss','accept','restore')),
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_org_suggestion_review_history_lookup
    ON organization_suggestion_review_history(library_id, suggestion_id, id);
