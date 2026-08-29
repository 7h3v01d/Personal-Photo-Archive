-- Personal Photo Archive — Migration 034: Phase 14.1 verified donor materialization
--
-- A successful row proves that the donor selected by a frozen Phase-13 proposal
-- was freshly revalidated and copied byte-for-byte into the already committed
-- Phase-14 preservation stage.  It does not authorise or perform target
-- replacement.

CREATE TABLE archive_recovery_donor_materializations (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    materialization_id           TEXT NOT NULL UNIQUE,
    stage_id                     TEXT NOT NULL UNIQUE
                                 REFERENCES archive_recovery_preservation_stages(stage_id) ON DELETE CASCADE,
    proposal_id                  TEXT NOT NULL
                                 REFERENCES archive_recovery_plan_proposals(proposal_id) ON DELETE CASCADE,
    target_file_id               TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    donor_file_id                TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    expected_revision_id         TEXT NOT NULL REFERENCES file_revisions(id) ON DELETE CASCADE,
    expected_sha256              TEXT NOT NULL,
    recovery_intent_resolution_id TEXT NOT NULL
                                 REFERENCES integrity_mismatch_resolutions(resolution_id) ON DELETE CASCADE,
    phase13_evidence_fingerprint TEXT NOT NULL,
    phase14_stage_fingerprint    TEXT NOT NULL,
    phase14_1_plan_fingerprint   TEXT NOT NULL,
    donor_source_path            TEXT NOT NULL,
    donor_materialization_path   TEXT NOT NULL,
    donor_materialized_sha256    TEXT NOT NULL,
    donor_materialized_size_bytes INTEGER NOT NULL,
    donor_manifest_path          TEXT NOT NULL,
    donor_manifest_sha256        TEXT NOT NULL,
    materialization_state        TEXT NOT NULL CHECK(materialization_state='verified_donor_materialized'),
    target_replacement_performed INTEGER NOT NULL DEFAULT 0 CHECK(target_replacement_performed=0),
    recovery_execution_authorized INTEGER NOT NULL DEFAULT 0 CHECK(recovery_execution_authorized=0),
    evidence_fingerprint         TEXT NOT NULL,
    note                         TEXT,
    materialized_at              TEXT NOT NULL
);

CREATE INDEX idx_archive_recovery_donor_target
    ON archive_recovery_donor_materializations(target_file_id, id DESC);
CREATE INDEX idx_archive_recovery_donor_expected
    ON archive_recovery_donor_materializations(expected_sha256);

CREATE TRIGGER trg_archive_recovery_donor_materialization_immutable
BEFORE UPDATE ON archive_recovery_donor_materializations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery donor materialization rows are immutable');
END;
