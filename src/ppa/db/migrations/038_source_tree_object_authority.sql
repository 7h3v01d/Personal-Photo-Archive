-- Phase 14.1.13 — Source-Tree Object Authority
--
-- Library-root identity is not sufficient to protect a child directory after
-- that child is renamed outside the registered root pathname. Persist every
-- directory object observed by a complete source-Library scan. These rows are
-- historical source-authority evidence: disappearance from the old pathname
-- does not retire the filesystem identity. Forgetting the owning Library is the
-- explicit retirement boundary (ON DELETE CASCADE).

ALTER TABLE libraries ADD COLUMN source_tree_identity_complete INTEGER NOT NULL DEFAULT 0
    CHECK (source_tree_identity_complete IN (0, 1));
ALTER TABLE libraries ADD COLUMN source_tree_identity_verified_at TEXT;

CREATE TABLE library_directory_identities (
    id                  INTEGER PRIMARY KEY,
    library_id          INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    canonical_path      TEXT NOT NULL,
    fs_device_id        TEXT NOT NULL,
    fs_object_id        TEXT NOT NULL,
    first_observed_at   TEXT NOT NULL,
    last_verified_at    TEXT NOT NULL,
    UNIQUE (library_id, fs_device_id, fs_object_id)
);

CREATE INDEX idx_library_directory_identity_object
    ON library_directory_identities (fs_device_id, fs_object_id);
CREATE INDEX idx_library_directory_identity_library
    ON library_directory_identities (library_id);

-- Object identity is historical authority and may not be rewritten in place.
-- Path/time fields may advance when the same object is re-observed.
CREATE TRIGGER library_directory_identity_no_rebind
BEFORE UPDATE OF fs_device_id, fs_object_id, library_id ON library_directory_identities
WHEN NEW.library_id <> OLD.library_id
  OR NEW.fs_device_id <> OLD.fs_device_id
  OR NEW.fs_object_id <> OLD.fs_object_id
BEGIN
    SELECT RAISE(ABORT, 'Library source-tree directory identity is immutable');
END;
