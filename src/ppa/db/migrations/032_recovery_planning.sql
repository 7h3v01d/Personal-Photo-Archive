-- Personal Photo Archive — Migration 032: Phase 13.0 recovery-plan proposals
--
-- Recovery planning is evidence/audit only.  A proposal records the exact target,
-- donor, immutable expected revision, stable physical observations, topology and
-- action sequence shown during review.  It does not authorise or perform a source
-- file write.  Phase 13.1+ must revalidate any proposal before considering an
-- execution boundary.

CREATE TABLE archive_recovery_plan_proposals (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id                TEXT NOT NULL UNIQUE,
    recovery_intent_resolution_id TEXT NOT NULL
                               REFERENCES integrity_mismatch_resolutions(resolution_id) ON DELETE CASCADE,
    target_file_id             TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    target_photo_id            TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    library_id                 INTEGER NOT NULL REFERENCES libraries(id),
    expected_revision_id       TEXT NOT NULL REFERENCES file_revisions(id) ON DELETE CASCADE,
    expected_sha256            TEXT NOT NULL,
    donor_file_id              TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    donor_photo_id             TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    donor_library_id           INTEGER NOT NULL REFERENCES libraries(id),
    donor_revision_id          TEXT NOT NULL REFERENCES file_revisions(id) ON DELETE CASCADE,
    donor_sha256               TEXT NOT NULL,
    target_path                TEXT NOT NULL,
    target_state               TEXT NOT NULL,
    target_observed_sha256     TEXT,
    target_size_bytes          INTEGER,
    target_mtime_ns            INTEGER,
    target_fs_device_id        TEXT,
    target_fs_object_id        TEXT,
    donor_path                 TEXT NOT NULL,
    donor_size_bytes           INTEGER NOT NULL,
    donor_mtime_ns             INTEGER NOT NULL,
    donor_fs_device_id         TEXT,
    donor_fs_object_id         TEXT,
    topology_class             TEXT NOT NULL,
    same_logical_photo         INTEGER NOT NULL CHECK(same_logical_photo IN (0,1)),
    same_library               INTEGER NOT NULL CHECK(same_library IN (0,1)),
    independent_backup_claim   INTEGER NOT NULL DEFAULT 0 CHECK(independent_backup_claim=0),
    proposed_action_json       TEXT NOT NULL,
    evidence_fingerprint       TEXT NOT NULL,
    note                       TEXT,
    proposal_state             TEXT NOT NULL DEFAULT 'dry_run_not_executed'
                               CHECK(proposal_state='dry_run_not_executed'),
    proposed_at                TEXT NOT NULL
);

CREATE INDEX idx_archive_recovery_proposals_target
    ON archive_recovery_plan_proposals(target_file_id, proposed_at DESC, id DESC);
CREATE INDEX idx_archive_recovery_proposals_donor
    ON archive_recovery_plan_proposals(donor_file_id, proposed_at DESC, id DESC);
CREATE INDEX idx_archive_recovery_proposals_expected
    ON archive_recovery_plan_proposals(expected_sha256);

CREATE TRIGGER trg_archive_recovery_proposal_immutable
BEFORE UPDATE ON archive_recovery_plan_proposals
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery proposal rows are immutable');
END;
