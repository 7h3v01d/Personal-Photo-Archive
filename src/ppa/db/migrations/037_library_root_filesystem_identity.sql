-- Phase 14.1.11 — Authority Bootstrap Binding
--
-- A Library's pathname is location, not filesystem identity.  Persist the
-- verified root-directory object so operational writers can reject that exact
-- source object even if it has been renamed away from its catalogue pathname.
-- Existing catalogues are backfilled lazily by the next successful scan; NULL
-- means "not yet object-attested", never permission to redefine identity.

ALTER TABLE libraries ADD COLUMN root_fs_device_id TEXT;
ALTER TABLE libraries ADD COLUMN root_fs_object_id TEXT;
ALTER TABLE libraries ADD COLUMN root_fs_verified_at TEXT;

-- Once established, normal catalogue activity must never silently redefine a
-- Library as a different filesystem object.  A future explicit relocation/rebind
-- workflow may deliberately replace these values, but ordinary UPDATEs fail.
CREATE TRIGGER libraries_root_identity_no_rebind
BEFORE UPDATE OF root_fs_device_id, root_fs_object_id ON libraries
WHEN OLD.root_fs_device_id IS NOT NULL
 AND OLD.root_fs_object_id IS NOT NULL
 AND (
      NEW.root_fs_device_id IS NULL
   OR NEW.root_fs_object_id IS NULL
   OR NEW.root_fs_device_id <> OLD.root_fs_device_id
   OR NEW.root_fs_object_id <> OLD.root_fs_object_id
 )
BEGIN
    SELECT RAISE(ABORT, 'registered Library filesystem identity is immutable without explicit rebind');
END;
