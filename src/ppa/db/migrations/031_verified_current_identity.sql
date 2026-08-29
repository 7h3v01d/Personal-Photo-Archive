-- Personal Photo Archive — Migration 031: verified-current identity hardening
--
-- Phase 12.4.1 makes mismatch-resolution review plans one-shot forensic
-- decisions.  Existing resolution rows predate decision identities and receive
-- unique legacy tokens solely so the uniqueness invariant is total.

ALTER TABLE integrity_mismatch_resolutions ADD COLUMN decision_id TEXT;

UPDATE integrity_mismatch_resolutions
   SET decision_id = 'legacy-' || lower(hex(randomblob(16)))
 WHERE decision_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mismatch_resolutions_decision_id
    ON integrity_mismatch_resolutions(decision_id);

CREATE TRIGGER IF NOT EXISTS trg_mismatch_resolutions_decision_id_required
BEFORE INSERT ON integrity_mismatch_resolutions
FOR EACH ROW
WHEN NEW.decision_id IS NULL OR trim(NEW.decision_id) = ''
BEGIN
    SELECT RAISE(ABORT, 'integrity mismatch resolution decision_id is required');
END;

CREATE TRIGGER IF NOT EXISTS trg_mismatch_resolutions_decision_id_immutable
BEFORE UPDATE OF decision_id ON integrity_mismatch_resolutions
FOR EACH ROW
WHEN NEW.decision_id IS NOT OLD.decision_id
BEGIN
    SELECT RAISE(ABORT, 'integrity mismatch resolution decision_id is immutable');
END;
