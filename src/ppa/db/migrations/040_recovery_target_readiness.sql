-- Personal Photo Archive — Migration 040: Phase 14.2 target-replacement readiness
--
-- This is an immutable planning/audit checkpoint only.  It proves that the
-- committed Phase-14.0 preservation evidence, Phase-14.1 donor materialization,
-- human recovery intent, target state, and current target-parent source-tree
-- identity were freshly re-attested.  It does NOT authorise or perform target
-- replacement.

CREATE TABLE archive_recovery_target_readiness (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    readiness_id                    TEXT NOT NULL UNIQUE,
    materialization_id              TEXT NOT NULL UNIQUE
                                    REFERENCES archive_recovery_donor_materializations(materialization_id),
    stage_id                        TEXT NOT NULL REFERENCES archive_recovery_preservation_stages(stage_id),
    proposal_id                     TEXT NOT NULL REFERENCES archive_recovery_plan_proposals(proposal_id),
    target_file_id                  TEXT NOT NULL REFERENCES files(id),
    library_id                      INTEGER NOT NULL REFERENCES libraries(id),
    expected_revision_id            TEXT NOT NULL REFERENCES file_revisions(id),
    expected_sha256                 TEXT NOT NULL,
    recovery_intent_resolution_id   TEXT NOT NULL REFERENCES integrity_mismatch_resolutions(resolution_id),
    target_path                     TEXT NOT NULL,
    target_state                    TEXT NOT NULL CHECK(target_state IN ('still_mismatched','unreadable','missing')),
    target_observed_sha256          TEXT,
    target_size_bytes               INTEGER,
    target_mtime_ns                 INTEGER,
    target_fs_device_id             TEXT,
    target_fs_object_id             TEXT,
    target_link_count               INTEGER,
    target_parent_path              TEXT NOT NULL,
    target_parent_fs_device_id      TEXT NOT NULL,
    target_parent_fs_object_id      TEXT NOT NULL,
    replacement_mode                TEXT NOT NULL CHECK(replacement_mode IN (
                                        'replace_existing_exact_target',
                                        'restore_missing_recorded_target'
                                    )),
    preservation_path               TEXT,
    preservation_sha256             TEXT,
    preservation_manifest_path      TEXT NOT NULL,
    preservation_manifest_sha256    TEXT NOT NULL,
    donor_materialization_path      TEXT NOT NULL,
    donor_materialized_sha256       TEXT NOT NULL,
    donor_materialized_size_bytes   INTEGER NOT NULL,
    donor_manifest_storage          TEXT NOT NULL CHECK(donor_manifest_storage IN ('filesystem_file','catalogue_embedded')),
    donor_manifest_path             TEXT NOT NULL,
    donor_manifest_sha256           TEXT NOT NULL,
    readiness_state                 TEXT NOT NULL CHECK(readiness_state='ready_for_replacement_protocol_review'),
    target_replacement_authorized   INTEGER NOT NULL DEFAULT 0 CHECK(target_replacement_authorized=0),
    recovery_execution_authorized   INTEGER NOT NULL DEFAULT 0 CHECK(recovery_execution_authorized=0),
    evidence_fingerprint            TEXT NOT NULL,
    note                            TEXT,
    assessed_at                     TEXT NOT NULL,
    CHECK (
        (target_state='missing' AND target_link_count IS NULL)
        OR
        (target_state<>'missing' AND target_link_count=1)
    )
);

CREATE INDEX idx_archive_recovery_target_readiness_file
    ON archive_recovery_target_readiness(target_file_id, assessed_at DESC, id DESC);

CREATE TRIGGER trg_archive_recovery_target_readiness_immutable
BEFORE UPDATE ON archive_recovery_target_readiness
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery target-readiness rows are immutable');
END;

CREATE TRIGGER trg_archive_recovery_target_readiness_delete_immutable
BEFORE DELETE ON archive_recovery_target_readiness
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery target-readiness rows are append-only and cannot be deleted');
END;
