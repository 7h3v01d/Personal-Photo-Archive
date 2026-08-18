-- Personal Photo Archive — SQLite Schema v1
-- Scope: Phase 0-2 (foundation, safe scanning, cryptographic identity/integrity).
--
-- Design rules this schema follows:
--   1. "Photo" (logical identity) is distinct from "File" (physical bytes on disk).
--      A Photo may have several Files (backup copy, resized export, etc.) once
--      duplicate/derivative detection lands in later phases.
--   2. Nothing here overwrites source files. This schema only ever records
--      observations *about* files — it never mutates them.
--   3. metadata_observations stores raw EXIF/filesystem facts as observations,
--      never as "the truth". Interpreted/reconciled dates belong to a later
--      table (metadata_interpretations, date_evidence — Phase 6/7) that is
--      deliberately NOT created yet, so it isn't designed against guesses.
--   4. Every row that represents a fact has a recorded source and timestamp,
--      so provenance survives even at the raw-observation layer.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Schema bookkeeping / migrations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- Import sessions — every scan/import run is logged (Phase 0 rule: every
-- interpretation/action records provenance; every import session is logged).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS import_sessions (
    id              TEXT PRIMARY KEY,          -- UUID
    library_path    TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    files_scanned   INTEGER NOT NULL DEFAULT 0,
    files_new       INTEGER NOT NULL DEFAULT 0,
    files_modified  INTEGER NOT NULL DEFAULT 0,
    files_missing   INTEGER NOT NULL DEFAULT 0,
    files_errored   INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

-- ---------------------------------------------------------------------------
-- Cameras — minimal identity now; camera profiles (clock reliability, active
-- years, etc. from Phase 25) are deliberately deferred.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cameras (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    make    TEXT,
    model   TEXT,
    serial  TEXT,
    UNIQUE (make, model, serial)
);

-- ---------------------------------------------------------------------------
-- Photos — logical identity, independent of filename/location/copy count.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS photos (
    id          TEXT PRIMARY KEY,      -- UUID, assigned once, never reused
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    notes       TEXT                   -- free-text user notes (Phase 27 will expand this)
);

-- ---------------------------------------------------------------------------
-- Files — physical representation of a photo. Read-only w.r.t. the source:
-- this table only ever describes what the scanner observed, never rewrites
-- the file itself.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS files (
    id                  TEXT PRIMARY KEY,      -- UUID
    photo_id            TEXT NOT NULL REFERENCES photos(id) ON DELETE RESTRICT,
    camera_id           INTEGER REFERENCES cameras(id),

    -- Location / identity on disk
    path                TEXT NOT NULL,         -- absolute path, current known location
    filename             TEXT NOT NULL,
    extension           TEXT,

    -- Observed filesystem facts (Phase 1)
    size_bytes          INTEGER NOT NULL,
    fs_mtime            TEXT,                  -- filesystem modification time, as observed
    fs_ctime            TEXT,
    width_px            INTEGER,
    height_px           INTEGER,
    mime_type           TEXT,

    -- Cryptographic identity (Phase 2)
    sha256              TEXT,
    hash_computed_at    TEXT,

    -- Lifecycle tracking
    status              TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'moved', 'missing', 'duplicate', 'error')),
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    first_seen_session  TEXT REFERENCES import_sessions(id),
    last_seen_session   TEXT REFERENCES import_sessions(id),

    -- If the scanner could not read the file at all
    inaccessible_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_photo_id   ON files(photo_id);
CREATE INDEX IF NOT EXISTS idx_files_sha256     ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_path       ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_status     ON files(status);

-- Path history — every observed path a file has occupied, so renames/moves
-- are auditable rather than silently overwritten.
CREATE TABLE IF NOT EXISTS file_path_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    session_id  TEXT REFERENCES import_sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_file_path_history_file_id ON file_path_history(file_id);

-- ---------------------------------------------------------------------------
-- Metadata observations — raw facts as read from EXIF/filesystem/filename.
-- Deliberately schemaless-ish (key/value) so Phase 3's extractor can capture
-- whatever a given file actually contains without a migration per field.
-- These are OBSERVATIONS, not interpretations — nothing here is "the" date.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS metadata_observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id      TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    source       TEXT NOT NULL,     -- e.g. 'exif', 'filesystem', 'filename'
    key          TEXT NOT NULL,     -- e.g. 'DateTimeOriginal', 'Make', 'GPSLatitude'
    value        TEXT,
    observed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    session_id   TEXT REFERENCES import_sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_metadata_obs_file_id ON metadata_observations(file_id);
CREATE INDEX IF NOT EXISTS idx_metadata_obs_key     ON metadata_observations(key);

-- ---------------------------------------------------------------------------
-- Integrity events — corruption warnings, hash mismatches, missing-file
-- detection (Phase 2/18). Append-only log; never deleted.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS integrity_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id      TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,     -- e.g. 'hash_mismatch', 'missing', 'corrupt', 'restored'
    detail       TEXT,
    occurred_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    session_id   TEXT REFERENCES import_sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_integrity_events_file_id ON integrity_events(file_id);

-- ---------------------------------------------------------------------------
-- NOT created yet, by design (see docs/ARCHIVE_SAFETY_CONTRACT.md and the
-- Phase 0 note above): metadata_interpretations, date_evidence, people,
-- faces, places, events, albums, tags, edits, duplicates, derivatives.
-- These get designed once Phase 1-4 have run against the real 10,000-photo
-- collection and we know what the data actually looks like.
-- ---------------------------------------------------------------------------

-- Schema version is recorded by the migration runner (ppa.db.connection),
-- not by the migration files themselves.
