-- Historical date reconstructions (schema v10).
--
-- A reconstruction is an INTERPRETATION of when a photo was actually taken,
-- produced by the Phase 7 engine from independent evidence (human anchors, GPS,
-- clock-offset propagation, bracketing). It is stored entirely separately from
-- observations and NEVER overwrites the recorded date or a photo's bytes — the
-- recorded (possibly wrong) date is preserved as evidence.
--
-- Lifecycle: the engine writes 'proposed' rows; a human confirms or rejects. A
-- confirmed reconstruction is authoritative for display/sort but still does not
-- mutate observations. Human decisions are sticky: re-running the engine refreshes
-- only 'proposed' rows and never overwrites a 'confirmed'/'rejected' decision.

CREATE TABLE IF NOT EXISTS reconstructions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_date  TEXT NOT NULL,                 -- YYYY-MM-DD
    end_date    TEXT,                          -- NULL => point date; else inclusive range
    confidence  TEXT NOT NULL CHECK (confidence IN ('confirmed','strong','range','proposed')),
    method      TEXT NOT NULL,                 -- direct|direct_gps|offset|anchor_range|bracket
    evidence    TEXT,
    status      TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','confirmed','rejected')),
    created_at  TEXT NOT NULL,
    decided_at  TEXT,                          -- when confirmed/rejected
    CHECK (end_date IS NULL OR end_date >= start_date),
    UNIQUE (file_id)                           -- one current reconstruction per file
);

CREATE INDEX IF NOT EXISTS idx_reconstructions_status ON reconstructions (status);
