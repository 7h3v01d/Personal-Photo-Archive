-- Personal Photo Archive — Migration 028: Ambiguous physical-File origin evidence
--
-- Phase 12.2 stops the scanner from arbitrarily selecting one historical File
-- when byte-identical catalogue candidates make a relocation/restoration origin
-- unknowable.  The newly observed File is catalogued honestly and this append-only
-- ledger records the candidate set that existed at observation time.
--
-- candidate_file_ids_json / candidate_photo_ids_json are canonical JSON arrays.
-- They are evidence snapshots, not mutable resolution state.  A later phase may
-- add an explicit human resolution layer without rewriting this observation.

CREATE TABLE file_origin_ambiguities (
    id                       TEXT PRIMARY KEY,
    library_id               INTEGER NOT NULL REFERENCES libraries(id),
    observed_file_id         TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    sha256                   TEXT NOT NULL,
    observed_path            TEXT NOT NULL,
    candidate_file_ids_json  TEXT NOT NULL,
    candidate_photo_ids_json TEXT NOT NULL,
    ambiguity_kind           TEXT NOT NULL CHECK (
        ambiguity_kind IN ('ambiguous_restoration', 'ambiguous_relocation')
    ),
    created_at               TEXT NOT NULL,
    session_id               TEXT REFERENCES import_sessions(id)
);

CREATE INDEX idx_file_origin_ambiguities_library
    ON file_origin_ambiguities(library_id, created_at);
CREATE UNIQUE INDEX idx_file_origin_ambiguities_observed_file
    ON file_origin_ambiguities(observed_file_id);
