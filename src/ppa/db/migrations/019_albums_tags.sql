-- Phase 9.0 — durable Albums & Tags foundation.
-- Organisation is human-authored catalogue state. It never becomes chronology evidence.

CREATE TABLE albums (
    id          TEXT PRIMARY KEY,
    library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_albums_library_name ON albums(library_id, name COLLATE NOCASE, id);

CREATE TABLE album_photos (
    album_id    TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    photo_id    TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    added_at    TEXT NOT NULL,
    PRIMARY KEY(album_id, photo_id)
);
CREATE INDEX idx_album_photos_photo ON album_photos(photo_id, album_id);

CREATE TABLE tags (
    id          TEXT PRIMARY KEY,
    library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_tags_library_name_ci ON tags(library_id, name COLLATE NOCASE);

CREATE TABLE photo_tags (
    tag_id      TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    photo_id    TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    added_at    TEXT NOT NULL,
    PRIMARY KEY(tag_id, photo_id)
);
CREATE INDEX idx_photo_tags_photo ON photo_tags(photo_id, tag_id);

CREATE TABLE organization_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    object_kind TEXT NOT NULL CHECK(object_kind IN ('album','tag')),
    object_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    photo_id    TEXT,
    old_value   TEXT,
    new_value   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_organization_history_object ON organization_history(object_kind, object_id, id);

-- Albums are Library-owned, while membership targets logical Photo identity.
-- A membership is legal only when that Photo has at least one File in the Album's Library.
CREATE TRIGGER album_photo_library_guard_insert
BEFORE INSERT ON album_photos
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM albums a
        JOIN files f ON f.photo_id = NEW.photo_id
        WHERE a.id = NEW.album_id AND f.library_id = a.library_id
    ) THEN RAISE(ABORT, 'album photo library mismatch') END;
END;

CREATE TRIGGER album_photo_library_guard_update
BEFORE UPDATE OF album_id, photo_id ON album_photos
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM albums a
        JOIN files f ON f.photo_id = NEW.photo_id
        WHERE a.id = NEW.album_id AND f.library_id = a.library_id
    ) THEN RAISE(ABORT, 'album photo library mismatch') END;
END;

CREATE TRIGGER photo_tag_library_guard_insert
BEFORE INSERT ON photo_tags
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM tags t
        JOIN files f ON f.photo_id = NEW.photo_id
        WHERE t.id = NEW.tag_id AND f.library_id = t.library_id
    ) THEN RAISE(ABORT, 'tag photo library mismatch') END;
END;

CREATE TRIGGER photo_tag_library_guard_update
BEFORE UPDATE OF tag_id, photo_id ON photo_tags
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM tags t
        JOIN files f ON f.photo_id = NEW.photo_id
        WHERE t.id = NEW.tag_id AND f.library_id = t.library_id
    ) THEN RAISE(ABORT, 'tag photo library mismatch') END;
END;
