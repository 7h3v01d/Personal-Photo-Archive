-- Personal Photo Archive — Migration 036: embedded Phase-14.1 orphan manifests
--
-- Windows cannot safely create a missing donor-manifest file inside an orphaned
-- recovery stage without directory-object-bound write authority.  A verified
-- orphan adoption may therefore retain the canonical manifest payload directly
-- in the append-only catalogue.  Normal materialisation continues to use a
-- filesystem manifest.

ALTER TABLE archive_recovery_donor_materializations
    ADD COLUMN donor_manifest_storage TEXT NOT NULL DEFAULT 'filesystem_file';

ALTER TABLE archive_recovery_donor_materializations
    ADD COLUMN donor_manifest_payload_json TEXT;

CREATE TRIGGER trg_archive_recovery_donor_manifest_storage_insert
BEFORE INSERT ON archive_recovery_donor_materializations
FOR EACH ROW
WHEN NEW.donor_manifest_storage NOT IN ('filesystem_file', 'catalogue_embedded')
     OR (NEW.donor_manifest_storage = 'catalogue_embedded' AND NEW.donor_manifest_payload_json IS NULL)
     OR (NEW.donor_manifest_storage = 'filesystem_file' AND NEW.donor_manifest_payload_json IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'invalid archive recovery donor manifest storage');
END;
