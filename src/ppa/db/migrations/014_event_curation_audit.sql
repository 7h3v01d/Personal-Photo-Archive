-- Phase 8.5 — auditable human event curation.
--
-- Event membership/name/note edits are human interpretation actions. Preserve
-- an append-only audit record without changing chronology evidence or source
-- photographs.

CREATE TABLE event_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('create','rename','note','add_member','remove_member')),
    file_id     TEXT,
    member_role TEXT,
    old_value   TEXT,
    new_value   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_event_history_event ON event_history(event_id, id);
