-- Backfill ownership for legacy anchors created before ownership was enforced
-- (schema v9). Only backfill where identity is DURABLE.
--
--   file scope: scope_ref is a persistent UUID -> files.id -> that file's
--               library_id. UUIDs are not reused, so this is provable.
--
-- Library anchors are NOT backfilled: their scope_ref is an integer library id,
-- and SQLite reuses those after a library is removed. A legacy anchor for a
-- removed Library A (id=1) would otherwise be reattached to an unrelated new
-- Library B that reused id=1 — recreating the very cross-resource contamination
-- ownership was added to prevent. Integer id alone is not provable provenance.
--
-- Directory anchors are likewise ambiguous (a path string can't identify a
-- library). Both are left unowned/dormant: retained for audit, not resolved
-- automatically, until a human reassigns them. Missing provenance is not global
-- provenance, and it is not guessable provenance either.

UPDATE anchors
SET library_id = (SELECT f.library_id FROM files f WHERE f.id = anchors.scope_ref)
WHERE scope = 'file'
  AND library_id IS NULL
  AND EXISTS (SELECT 1 FROM files f WHERE f.id = anchors.scope_ref);
