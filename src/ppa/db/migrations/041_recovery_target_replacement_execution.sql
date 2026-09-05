-- Personal Photo Archive — Migration 041: Phase 14.3 target-replacement execution
--
-- This is the first recovery phase that may intentionally mutate the registered
-- source-photo namespace.  Authority is represented by one immutable, explicitly
-- confirmed execution attempt derived from one immutable Phase-14.2 readiness
-- checkpoint.  Results are append-only and separate from attempts so a crash
-- between authorization and result commit remains visible as an unresolved
-- attempt rather than being silently replayable.

CREATE TABLE archive_recovery_target_execution_attempts (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id                    TEXT NOT NULL UNIQUE,
    readiness_id                    TEXT NOT NULL UNIQUE
                                    REFERENCES archive_recovery_target_readiness(readiness_id),
    materialization_id              TEXT NOT NULL
                                    REFERENCES archive_recovery_donor_materializations(materialization_id),
    target_file_id                  TEXT NOT NULL REFERENCES files(id),
    library_id                      INTEGER NOT NULL REFERENCES libraries(id),
    expected_revision_id            TEXT NOT NULL REFERENCES file_revisions(id),
    expected_sha256                 TEXT NOT NULL,
    recovery_intent_resolution_id   TEXT NOT NULL REFERENCES integrity_mismatch_resolutions(resolution_id),
    replacement_mode                TEXT NOT NULL CHECK(replacement_mode IN (
                                        'replace_existing_exact_target',
                                        'restore_missing_recorded_target'
                                    )),
    target_path                     TEXT NOT NULL,
    target_initial_state            TEXT NOT NULL CHECK(target_initial_state IN ('still_mismatched','unreadable','missing')),
    target_initial_sha256           TEXT,
    target_initial_size_bytes       INTEGER,
    target_initial_mtime_ns         INTEGER,
    target_initial_fs_device_id     TEXT,
    target_initial_fs_object_id     TEXT,
    target_initial_link_count       INTEGER,
    library_root_path               TEXT NOT NULL,
    library_root_fs_device_id       TEXT NOT NULL,
    library_root_fs_object_id       TEXT NOT NULL,
    target_parent_path              TEXT NOT NULL,
    target_parent_fs_device_id      TEXT NOT NULL,
    target_parent_fs_object_id      TEXT NOT NULL,
    donor_materialization_path      TEXT NOT NULL,
    donor_materialized_sha256       TEXT NOT NULL,
    donor_materialized_size_bytes   INTEGER NOT NULL,
    readiness_evidence_fingerprint  TEXT NOT NULL,
    execution_plan_fingerprint      TEXT NOT NULL,
    confirmation_phrase_sha256      TEXT NOT NULL,
    authorization_state             TEXT NOT NULL CHECK(authorization_state='confirmed_one_attempt'),
    target_replacement_authorized   INTEGER NOT NULL CHECK(target_replacement_authorized=1),
    recovery_execution_authorized   INTEGER NOT NULL CHECK(recovery_execution_authorized=1),
    note                            TEXT,
    authorized_at                   TEXT NOT NULL,
    CHECK (
        (target_initial_state='missing' AND target_initial_link_count IS NULL)
        OR
        (target_initial_state<>'missing' AND target_initial_link_count=1)
    )
);

CREATE INDEX idx_archive_recovery_target_execution_attempt_file
    ON archive_recovery_target_execution_attempts(target_file_id, authorized_at DESC, id DESC);

CREATE TABLE archive_recovery_target_execution_results (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id                       TEXT NOT NULL UNIQUE,
    execution_id                    TEXT NOT NULL UNIQUE
                                    REFERENCES archive_recovery_target_execution_attempts(execution_id),
    readiness_id                    TEXT NOT NULL
                                    REFERENCES archive_recovery_target_readiness(readiness_id),
    target_file_id                  TEXT NOT NULL REFERENCES files(id),
    result_state                    TEXT NOT NULL CHECK(result_state IN (
                                        'expected_target_placed_verified',
                                        'aborted_before_target_transition',
                                        'aborted_exact_target_restored'
                                    )),
    installed_sha256                TEXT,
    installed_size_bytes            INTEGER,
    installed_fs_device_id          TEXT,
    installed_fs_object_id          TEXT,
    installed_link_count            INTEGER,
    suspect_retained_path           TEXT,
    suspect_sha256                  TEXT,
    suspect_size_bytes              INTEGER,
    suspect_fs_device_id            TEXT,
    suspect_fs_object_id            TEXT,
    source_namespace_changed        INTEGER NOT NULL CHECK(source_namespace_changed IN (0,1)),
    verify_reconciliation_required  INTEGER NOT NULL CHECK(verify_reconciliation_required IN (0,1)),
    evidence_fingerprint            TEXT NOT NULL,
    detail                          TEXT,
    completed_at                    TEXT NOT NULL,
    CHECK (
        (result_state='expected_target_placed_verified'
         AND installed_sha256 IS NOT NULL
         AND installed_size_bytes IS NOT NULL
         AND installed_fs_device_id IS NOT NULL
         AND installed_fs_object_id IS NOT NULL
         AND installed_link_count=1
         AND verify_reconciliation_required=1)
        OR
        (result_state<>'expected_target_placed_verified'
         AND verify_reconciliation_required=0)
    )
);

CREATE INDEX idx_archive_recovery_target_execution_result_file
    ON archive_recovery_target_execution_results(target_file_id, completed_at DESC, id DESC);

CREATE TRIGGER trg_archive_recovery_target_execution_attempt_immutable
BEFORE UPDATE ON archive_recovery_target_execution_attempts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery target execution attempts are immutable');
END;

CREATE TRIGGER trg_archive_recovery_target_execution_attempt_delete_immutable
BEFORE DELETE ON archive_recovery_target_execution_attempts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery target execution attempts are append-only and cannot be deleted');
END;

CREATE TRIGGER trg_archive_recovery_target_execution_result_immutable
BEFORE UPDATE ON archive_recovery_target_execution_results
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery target execution results are immutable');
END;

CREATE TRIGGER trg_archive_recovery_target_execution_result_delete_immutable
BEFORE DELETE ON archive_recovery_target_execution_results
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery target execution results are append-only and cannot be deleted');
END;
