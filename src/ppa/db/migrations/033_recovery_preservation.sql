-- Personal Photo Archive — Migration 033: Phase 14.0 recovery preservation staging
--
-- A successful row proves that a frozen Phase-13 proposal was freshly
-- revalidated and, when suspect target bytes existed, that an exact byte-for-byte
-- preservation copy was written to PPA operational storage.  This table does not
-- authorise target replacement or donor materialisation.

CREATE TABLE archive_recovery_preservation_stages (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id                     TEXT NOT NULL UNIQUE,
    proposal_id                  TEXT NOT NULL UNIQUE
                                 REFERENCES archive_recovery_plan_proposals(proposal_id) ON DELETE CASCADE,
    target_file_id               TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    target_photo_id              TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    library_id                   INTEGER NOT NULL REFERENCES libraries(id),
    donor_file_id                TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    expected_revision_id         TEXT NOT NULL REFERENCES file_revisions(id) ON DELETE CASCADE,
    expected_sha256              TEXT NOT NULL,
    recovery_intent_resolution_id TEXT NOT NULL
                                 REFERENCES integrity_mismatch_resolutions(resolution_id) ON DELETE CASCADE,
    phase13_evidence_fingerprint TEXT NOT NULL,
    phase14_plan_fingerprint     TEXT NOT NULL,
    target_path                  TEXT NOT NULL,
    target_state                 TEXT NOT NULL,
    target_observed_sha256       TEXT,
    target_size_bytes            INTEGER,
    target_mtime_ns              INTEGER,
    target_fs_device_id          TEXT,
    target_fs_object_id          TEXT,
    donor_path                   TEXT NOT NULL,
    donor_observed_sha256        TEXT NOT NULL,
    donor_size_bytes             INTEGER NOT NULL,
    donor_mtime_ns               INTEGER NOT NULL,
    donor_fs_device_id           TEXT,
    donor_fs_object_id           TEXT,
    preservation_root            TEXT NOT NULL,
    preservation_path            TEXT,
    preserved_sha256             TEXT,
    preserved_size_bytes         INTEGER,
    manifest_path                TEXT NOT NULL,
    manifest_sha256              TEXT NOT NULL,
    stage_state                  TEXT NOT NULL CHECK(stage_state IN (
                                     'suspect_bytes_preserved',
                                     'target_missing_no_preservation_required'
                                 )),
    target_replacement_performed INTEGER NOT NULL DEFAULT 0 CHECK(target_replacement_performed=0),
    donor_materialized           INTEGER NOT NULL DEFAULT 0 CHECK(donor_materialized=0),
    recovery_execution_authorized INTEGER NOT NULL DEFAULT 0 CHECK(recovery_execution_authorized=0),
    evidence_fingerprint         TEXT NOT NULL,
    note                         TEXT,
    staged_at                    TEXT NOT NULL,
    CHECK (
        (stage_state='suspect_bytes_preserved'
         AND preservation_path IS NOT NULL
         AND preserved_sha256 IS NOT NULL
         AND preserved_size_bytes IS NOT NULL)
        OR
        (stage_state='target_missing_no_preservation_required'
         AND preservation_path IS NULL
         AND preserved_sha256 IS NULL
         AND preserved_size_bytes IS NULL)
    )
);

CREATE INDEX idx_archive_recovery_preservation_target
    ON archive_recovery_preservation_stages(target_file_id, id DESC);
CREATE INDEX idx_archive_recovery_preservation_expected
    ON archive_recovery_preservation_stages(expected_sha256);

CREATE TRIGGER trg_archive_recovery_preservation_immutable
BEFORE UPDATE ON archive_recovery_preservation_stages
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery preservation stage rows are immutable');
END;
