-- Phase 14.1.14 — Positive Operational Authority
-- Writable operational objects are trusted because PPA explicitly created or
-- enrolled their exact filesystem identities for a purpose, never merely because
-- they are absent from source-history deny lists.
CREATE TABLE operational_directories (
    id INTEGER PRIMARY KEY,
    purpose TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    fs_device_id TEXT NOT NULL,
    fs_object_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    UNIQUE(purpose, canonical_path),
    UNIQUE(fs_device_id, fs_object_id)
);
CREATE INDEX idx_operational_directory_purpose ON operational_directories(purpose);

CREATE TABLE operational_files (
    id INTEGER PRIMARY KEY,
    purpose TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    fs_device_id TEXT NOT NULL,
    fs_object_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    UNIQUE(purpose, canonical_path),
    UNIQUE(fs_device_id, fs_object_id)
);
CREATE INDEX idx_operational_file_purpose ON operational_files(purpose);
