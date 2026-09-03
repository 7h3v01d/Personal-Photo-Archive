-- Personal Photo Archive — Migration 035: recovery checkpoint delete immutability
--
-- Phase 13/14 recovery proposal, preservation, and donor-materialization rows are
-- append-only forensic checkpoints.  Earlier migrations prohibited UPDATE but
-- still allowed direct DELETE (and parent ON DELETE CASCADE) to erase evidence.
-- Deletion now fails closed.  Any future archival-retirement semantics must be
-- explicit and separately audited rather than silently cascading checkpoints.

CREATE TRIGGER trg_archive_recovery_proposal_delete_immutable
BEFORE DELETE ON archive_recovery_plan_proposals
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery proposal rows are append-only and cannot be deleted');
END;

CREATE TRIGGER trg_archive_recovery_preservation_delete_immutable
BEFORE DELETE ON archive_recovery_preservation_stages
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery preservation stage rows are append-only and cannot be deleted');
END;

CREATE TRIGGER trg_archive_recovery_donor_materialization_delete_immutable
BEFORE DELETE ON archive_recovery_donor_materializations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'archive recovery donor materialization rows are append-only and cannot be deleted');
END;
