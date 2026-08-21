-- Anchor ownership (schema v8).
--
-- Anchors previously identified their subject only by (scope, scope_ref). For
-- 'library' scope that ref is the library's integer id, which SQLite REUSES
-- after a library is removed — so a forgotten library's anchor could attach to
-- an unrelated new library that happened to get the same id, leaking
-- authoritative human date evidence across resources. For 'directory' scope,
-- identically named folders in different libraries could also collide.
--
-- Giving every anchor a durable owning library_id fixes both: removal deletes an
-- owning library's anchors cleanly, and resolution can be scoped to the owner.
-- Nullable so pre-existing rows migrate without a backfill; new anchors set it.

ALTER TABLE anchors ADD COLUMN library_id INTEGER REFERENCES libraries(id);

CREATE INDEX IF NOT EXISTS idx_anchors_library ON anchors (library_id);
