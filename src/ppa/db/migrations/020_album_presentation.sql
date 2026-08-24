-- Phase 9.3 — Album presentation preferences over logical Photo identity.
CREATE TABLE album_presentation (
    album_id        TEXT PRIMARY KEY REFERENCES albums(id) ON DELETE CASCADE,
    cover_photo_id  TEXT REFERENCES photos(id) ON DELETE SET NULL,
    order_json      TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE album_presentation_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id    TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('cover','order','reset')),
    old_value   TEXT,
    new_value   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_album_presentation_history_album ON album_presentation_history(album_id,id);

CREATE TRIGGER album_presentation_cover_guard_insert
BEFORE INSERT ON album_presentation
WHEN NEW.cover_photo_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM album_photos ap WHERE ap.album_id=NEW.album_id AND ap.photo_id=NEW.cover_photo_id
    ) THEN RAISE(ABORT, 'album cover must be a current album member') END;
END;

CREATE TRIGGER album_presentation_cover_guard_update
BEFORE UPDATE OF cover_photo_id ON album_presentation
WHEN NEW.cover_photo_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM album_photos ap WHERE ap.album_id=NEW.album_id AND ap.photo_id=NEW.cover_photo_id
    ) THEN RAISE(ABORT, 'album cover must be a current album member') END;
END;

CREATE TRIGGER album_member_presentation_cleanup_delete
AFTER DELETE ON album_photos
BEGIN
    UPDATE album_presentation
       SET cover_photo_id=CASE WHEN cover_photo_id=OLD.photo_id THEN NULL ELSE cover_photo_id END,
           order_json=NULL,
           updated_at=datetime('now')
     WHERE album_id=OLD.album_id;
END;

CREATE TRIGGER album_member_presentation_cleanup_insert
AFTER INSERT ON album_photos
BEGIN
    UPDATE album_presentation SET order_json=NULL, updated_at=datetime('now')
     WHERE album_id=NEW.album_id;
END;
