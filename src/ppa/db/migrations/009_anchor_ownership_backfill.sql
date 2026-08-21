-- Backfill ownership for legacy anchors created before ownership was enforced
-- (schema v9). Only deterministically recoverable cases are filled:
--
--   library scope: scope_ref IS the library id -> adopt it (if that library
--                  still exists).
--   file scope:    scope_ref -> files.id -> that file's library_id.
--
-- Directory anchors are intentionally left unowned: their path-like scope_ref
-- cannot identify an owning library, and guessing could reattach human evidence
-- to the wrong resource. Unowned rows are retained for audit but resolve_for()
-- no longer applies them automatically; they stay dormant until reassigned.

UPDATE anchors
SET library_id = CAST(scope_ref AS INTEGER)
WHERE scope = 'library'
  AND library_id IS NULL
  AND CAST(scope_ref AS INTEGER) IN (SELECT id FROM libraries);

UPDATE anchors
SET library_id = (SELECT f.library_id FROM files f WHERE f.id = anchors.scope_ref)
WHERE scope = 'file'
  AND library_id IS NULL
  AND EXISTS (SELECT 1 FROM files f WHERE f.id = anchors.scope_ref);
