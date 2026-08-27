# Personal Photo Archive

Local-first digital photography management and preservation platform.
See `docs/ARCHIVE_SAFETY_CONTRACT.md` for the non-negotiable rules every
feature is built against.

Status: **Archive Core ACCEPTED; Phase 6 FROZEN; Phases 7–10 delivered; Phase 11 navigation refactor complete; Phase 12.1 Backup & Archive Health storage-identity layer active.**

The Library -> File -> FileRevision -> Observation model remains the archive-core
provenance boundary. Later chronology, Event, organisation, identity, navigation,
and Archive Health layers build on that evidence without rewriting source photos.
Current content identity is revision-bound; legacy `status` / `files.sha256` fields
remain maintained compatibility mirrors rather than the preferred authority.

See `docs/HARDENING.md` for the Archive Core findings -> fix -> test map, and `docs/PHASE8_14_1_PRE_PHASE9_HARDENING.md` for the independent pre-Phase-9 UI/integration hardening pass. Per the reviewer, Phase 6 (Date Reliability Engine) can
unblock once a confirming adversarial pass on this build comes back clean.
Source-file safety was never implicated: no path writes to originals.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

## Run

```bash
python -m ppa.main
```

On first run this will:
- create `~/.config/personal-photo-archive/config.toml` if it doesn't exist
- create the catalogue database at the path in that config
- open the desktop window with compact **Library / Timeline / Organisation / Identity / Diagnostics** workspaces, the **Commands…** palette, **Refresh**, and a grid-size control. The Library workspace includes **Add Library…**, **Scan**, **Verify**, **Archive Health**, and **Extract Metadata**; the main catalogue surface remains a thumbnail grid
  (All / Recently Added / Duplicates / Missing) with status-tinted tiles —
  a red MISSING ribbon on absent files, an amber ×N badge on duplicate
  copies, a teal selection ring; and an inspector showing each file's
  identity, dimensions, SHA-256, copy count, observed EXIF metadata, an
  offline GPS mini-map, integrity-event history, and Open-folder /
  Copy-path actions

Scans and verifies run on background threads, so a large library never
freezes the UI. Thumbnails are cached on disk, keyed by SHA-256, so
duplicate copies share one thumbnail and re-browsing is instant.

## Scan a library (no GUI needed)

```bash
python -m ppa.cli scan /path/to/your/sample_library
```

This is the fastest way to run the scanner against a real slice of your
collection and see what it reports before any UI exists. It only ever
reads from the directory you point it at — see
`docs/ARCHIVE_SAFETY_CONTRACT.md`.

## Read metadata

```bash
python -m ppa.cli extract
```

Reads embedded EXIF (camera make/model/serial, DateTimeOriginal, ISO,
aperture, focal length, lens, GPS) plus a filesystem date into the
catalogue as **observations** — (source, key, value) rows in
`metadata_observations`, never written back into the file, and never
treated as the photo's true capture date. Extraction is idempotent and
hash-aware: a file is only re-read when its content changes, and stale
observations are replaced rather than accumulated. The desktop app runs
this automatically after each scan.

## Pilot analysis report (Phase 7.2.1)

```bash
python -m ppa.cli pilot report 1
python -m ppa.cli pilot report 1 --directory 2001-2006
python -m ppa.cli pilot report 1 --json pilot-report.json
```

The pilot report is **read-only**. It aggregates the accepted date-reliability,
sequence, independent-evidence, reconstruction and staleness layers without
creating anchors, proposals, decisions or new chronology claims. Every aggregate
contains the exact file IDs behind it, and JSON output is versioned as
`ppa-pilot-report/1` for repeatable before/after pilot comparisons.

## Prioritised date review queue (Phase 7.2.2)

```bash
python -m ppa.cli pilot queue 1
python -m ppa.cli pilot queue 1 --directory 2001-2006
python -m ppa.cli pilot queue 1 --json date-review-queue.json
python -m ppa.cli pilot queue 1 --all
```

The queue is also available from **Date Review** in the desktop toolbar. It is a
read-only prioritisation layer: Priority A/B/C items are presented in deterministic
order with an explicit reason, while all confirm/reject/reopen/refresh actions still
go through the hardened Phase-7 persistence API. Priority D files are omitted from
the normal interactive queue so low-information photos do not bury useful review
work. Queue JSON is versioned as `ppa-date-review-queue/1`.

On a real library the chronology/evidence/freshness pass can be substantial. Desktop
queue construction therefore runs on a dedicated worker with its own SQLite
connection; an indeterminate progress dialog and status messages keep the UI live,
and cancellation is checked cooperatively throughout the reporting pass. The pilot
metadata-quality summary uses one library-scoped observation query rather than a
per-photo SQL loop.

## Verify integrity (detect silent corruption)

```bash
python -m ppa.cli verify
```

Re-hashes every catalogued file and compares against the recorded
SHA-256. A scan trusts a file's stored hash when its size and mtime are
unchanged (so routine re-scans of 10,000+ photos stay fast), which means
a scan will not notice *silent* corruption — bit rot, a bad sector, or a
tool that rewrote the bytes while preserving the timestamp. `verify` is
the deliberate, re-read-everything check for exactly that. It never
repairs anything and never overwrites a stored hash on mismatch: a
mismatch is a warning to investigate against your backups, logged to
`integrity_events`.

HEIC files are detected but reported as unsupported unless the optional
plugin is installed:

```bash
pip install -e ".[heic]"
```

## Test

```bash
pytest
```

## Layout

```text
src/ppa/
    config.py          Config loading (TOML)
    logging_setup.py    Logging (console + rotating file)
    main.py             App entry point (PySide6)
    cli.py               CLI (`python -m ppa.cli scan <path>`)
    formats.py           Supported/deferred format registry
    hashing.py           SHA-256 content hashing (Phase 2)
    scanner.py            Safe library scanner (Phase 1 + hash-aware Phase 2)
    integrity.py          Re-verification / corruption detection (Phase 2)
    catalogue.py          Read model: DB -> typed dataclasses (no Qt)
    metadata.py           EXIF/GPS/filesystem observation extractor (Phase 3)
    thumbnails.py         SHA-256-keyed thumbnail cache (no Qt)
    ui/
        theme.py         Dark industrial palette + QSS
        workers.py       Scan/verify/metadata/thumbnail workers
        models.py        Thumbnail grid model (lazy loading)
        delegate.py      Grid tile painting: status tint, badges, selection
        gpsmap.py        Offline schematic GPS mini-map
        main_window.py   Nav / grid / inspector window
    db/
        migrations/
            001_initial.sql
            002_libraries.sql
            003_revisions.sql
            004_ownership.sql
            005_identity.sql
            006_unique_identity.sql
        migrations/001_initial.sql       SQLite schema v1
        connection.py    DB open/init
tests/                  pytest suite
docs/
    ARCHIVE_SAFETY_CONTRACT.md
data/sample_library/    Real (small, personal) photo subset for dev/testing —
                         not committed to git, see .gitignore
```

## How identity works (Phase 2)

The scanner reconciles the filesystem against the catalogue by **content
hash** (SHA-256), so it can tell these apart as facts rather than guesses:

- **unchanged** — same path, same content
- **modified** — same path, content changed (caught even if the byte size
  is identical)
- **moved** — content that used to live at one path now lives only at
  another (rename and/or relocation, confirmed by hash)
- **duplicate** — the same content exists in two places at once; the copy
  becomes another *File* of the same logical *Photo* rather than a second
  Photo
- **missing** — a catalogued file's path is gone and its content wasn't
  found elsewhere (marked missing, never deleted from the catalogue)
- **restored** — a previously-missing file's content reappeared

The scan is two-pass on purpose: telling a *move* apart from a *duplicate*
requires knowing whether the original path still exists, which isn't
knowable until the whole tree has been walked. Pass 1 inventories and
hashes; Pass 2 reconciles. Every path change is written to
`file_path_history` and every notable transition to `integrity_events`,
so nothing is silently overwritten.

### Phase 7.2.3 — Anchor opportunities

PPA can now rank the highest-value human date question in a library or historical subset. The planner is deterministic/read-only and only uses strong-device reset groups where one exact human clue could constrain other unresolved frames.

```text
python -m ppa.cli pilot questions 1
python -m ppa.cli pilot questions 1 --json anchor-opportunities.json
```

The desktop **Date Review** workflow uses the same ranking and labels the leading high-leverage frame as **Best date question**, including how many other photographs the answer could help.


### Phase 7.2.4 — Evidence Inspector

Date Review now has a **Why?** action that builds a read-only, structured trace of the current Phase-6/7 reasoning on a background worker: recorded timestamp, reliability reasons, chronology findings, independent anchors/GPS evidence, reset-run/device basis, reconstruction method, derivation, and stale state.

```text
python -m ppa.cli pilot explain <file-id>
python -m ppa.cli pilot explain <file-id> --json evidence.json
```

See `docs/PHASE7_2_4_EVIDENCE_INSPECTOR.md`.

### Phase 7.2.5 — controlled batch confirmation

Date Review can now offer **Review batch…** for a strictly eligible strong-device
clock-reset run. PPA shows distributed visual spot-checks, requires explicit human
acknowledgement, then revalidates the complete revision/evidence-bound plan and
confirms every member atomically. Stale, partial, ambiguous, range/bracket, or mixed-
decision runs fail closed. See `docs/PHASE7_2_5_CONTROLLED_BATCH_CONFIRMATION.md`.

### Phase 7.2.6 — Unresolved Memories

The desktop **Unresolved Memories** action and `ppa pilot unresolved` classify photos PPA cannot responsibly date into explicit, traceable categories rather than fabricating precision. Classification is read-only and runs off the GUI thread. See `docs/PHASE7_2_6_UNRESOLVED_MEMORIES.md`.

## Phase 7.2.7 — Pilot Audit

The desktop **Pilot Audit** action and `ppa pilot audit` CLI capture a read-only,
traceable chronology snapshot. Save snapshots as JSON and compare two explicit
same-scope runs with `ppa pilot audit-compare before.json after.json`. PPA never
fabricates a historical “before” state from the current catalogue.

### Phase 7.3 — real-collection pilot sessions

A durable pilot harness can now capture a truthful baseline, append checkpoints,
and close with a final Phase-7 audit comparison without modifying source photos or
chronology evidence:

```bash
python -m ppa.cli pilot session-start 1 pilot.json --directory 2001-2006
python -m ppa.cli pilot session-checkpoint pilot.json --label "after review session 1"
python -m ppa.cli pilot session-status pilot.json
python -m ppa.cli pilot session-close pilot.json
```

See `docs/PHASE7_3_REAL_COLLECTION_PILOT.md`.

### Phase 7.4 — Pilot Session Dashboard

The desktop toolbar now includes **Pilot Session…**, a guided operator surface for Phase 7.3 sessions. It can start/load a session, refresh baseline-relative progress, launch Date Review or Unresolved Memories inside the session's exact validated scope, capture named checkpoints, and close the pilot with a final comparison. Collection-wide work runs off the Qt GUI thread. See `docs/PHASE7_4_PILOT_DASHBOARD.md`.

## Phase 7.5 — activity log and shareable diagnostics

PPA now writes a human-readable rotating `ppa.log` and a structured `ppa.jsonl` companion. Use **Activity Log…** in the desktop toolbar to watch recent activity live, or **Export Diagnostics…** to create a sanitized ZIP suitable for sharing while troubleshooting. The diagnostics export excludes the catalogue database, photos, thumbnails, pilot-session artifacts, and raw configuration paths.

CLI equivalents:

```text
python -m ppa.cli diagnostics tail --lines 200
python -m ppa.cli diagnostics export ppa-diagnostics.zip
```

### Phase 7.6 — correlated activity runs
The desktop **Activity Runs…** view groups Phase-7 operations by run ID with start,
progress, terminal outcome, and duration. Use `ppa diagnostics runs` and
`ppa diagnostics run-export <run-id> <file.json>` for CLI monitoring/sharing.

### Phase 7.7 — shareable review progress

From **Pilot Session…**, use **Share progress…** to export a sanitized ZIP containing baseline→current
chronology metrics, checkpoint progression, integrity status, and matching scoped operational-run summaries.
No photos, catalogue database, raw paths, photo IDs, or raw log messages are included.

CLI: `python -m ppa.cli pilot session-report pilot.json progress.zip`


## Phase 8.0 — Chronology Timeline Foundation

The desktop **Timeline** action builds a read-only, provenance-aware chronology view in the background. Photos are separated into **Placed**, **Ranges**, **Tentative**, and **Unplaced** lanes. Fresh human-confirmed reconstructions take precedence; otherwise current TRUSTED/PROBABLY_VALID reconciled chronology may place a photo. Fresh proposals are tentative only, and stale decisions never anchor the timeline. Date ranges remain ranges.

CLI: `python -m ppa.cli timeline <library_id>` with optional `--directory` and `--json`.

## Phase 8.2 — Timeline Scale & Fast Jumping

The desktop Timeline now supports **Decades / Years / Months** density navigation,
fast page scrubbing, Previous/Next paging, and bounded thumbnail materialisation.
The full chronology projection remains immutable in memory, while the visual grid
loads at most 120 catalogue/thumbnail items per lane at a time. Range precision,
placement authority, tentative chronology, and unplaced segregation remain exactly
as defined by Phase 8.0.

### Phase 8.3 — Timeline Context

Timeline navigation can now detect conservative provisional chronological
clusters (`Clusters` scale) from already-authorised point dates. Tentative/range
items may be shown as context but never seed or strengthen a cluster. Use
`python -m ppa.cli timeline-clusters <library_id>` for the read-only CLI view.

### Phase 8.4 — Human Event Identity

Timeline provisional clusters can now be explicitly named by a human. Naming a
cluster creates a durable Event UUID with a snapshot of its authoritative seed
photos; it does not rename the clustering algorithm or promote tentative/range
context into event membership. The Timeline **Events** scale browses these
human-authored event identities independently of future cluster changes.

### Phase 8.5 — Event curation

Named Timeline Events can now be explicitly curated from **Timeline → Events → Edit event…**.
Rename the event, add a human note, add/remove same-Library photos, and inspect the recent audit
history. Event membership never changes a photo's chronology lane or metadata. Human edits are
append-audited in schema v14.

## Phase 8.6 — Event Story Context

Named Events can now carry richer human-authored memory: description, remembered place, people notes, occasion/context, and a longer story. These fields are stored separately from chronology evidence and are append-versioned in `event_context_history`. Editing story context never changes Timeline placement, date reliability, reconstruction state, EXIF, metadata observations, or source photos.

See `docs/PHASE8_6_EVENT_STORY_CONTEXT.md`.

## Phase 8.7 — Event Browse / Story View

Timeline named Events now have **Story view…**, an album-style read-only presentation
of human story context plus the Event's explicitly curated member photos. Members are
ordered by their **current** Timeline chronology; ranges, tentative dates, stale states,
and unplaced photos retain their existing truth state. Event membership and narrative
context never become chronology evidence. The visual story is paged to at most 120
thumbnail items at once and thumbnail decoding stays off the GUI thread.

CLI: `python -m ppa.cli event-story <event-uuid>` with optional `--json`.
See `docs/PHASE8_7_EVENT_STORY_VIEW.md`.

## Phase 8.8 — Event-to-Event Story Navigation

Story View now supports deterministic Previous/Next Event reading across year-grouped durable human Events. The navigation layer is read-only and never changes Event membership, Story Context, or chronology authority. See `docs/PHASE8_8_EVENT_STORY_NAVIGATION.md`.

## Phase 8.9 — Family History Home

The desktop toolbar now includes **Family History**, a year-grouped visual landing page
for durable human Events. Event cards show a semantically neutral stable cover thumbnail,
date span, member/chronology counts, occasion/place context, and a short story excerpt.
Only 30 Event cards/covers are materialised per page. Double-click a card or choose
**Open story…** to enter the existing continuous Story View. Cover selection is a stable
browsing default only and never chronology or importance evidence.

CLI: `python -m ppa.cli event-home <library_id>` with optional `--json`.
See `docs/PHASE8_9_FAMILY_HISTORY_HOME.md`.

## Phase 8.10 — Human Cover Selection & Presentation Preferences

Human Events now support optional display-only presentation preferences:

- choose any current Event member as the preferred Family History cover;
- arrange the complete current Event membership into a custom Story reading order;
- reset both choices back to deterministic defaults;
- retain append-only presentation history.

These preferences are intentionally outside chronology authority. Choosing a
cover or moving a photo earlier in a Story never changes its date, Timeline
lane, reliability, reconstruction, Event membership, metadata, EXIF, or source
bytes. A custom order must be an exact permutation of current Event members.
Membership changes invalidate the old order rather than guessing how new or
removed members should be placed. A removed preferred-cover member clears the
cover preference automatically.

Schema v16 adds `event_presentation` and `event_presentation_history` plus a
DB-level guard that prevents a non-member photo from becoming an Event cover.

## Phase 8.11 — Event Search & Discovery

**Family History** now supports deterministic read-only search across Event name,
occasion, remembered place, people notes, description, story text, and the short
curation note. Multiple search tokens use AND semantics and results report which
human-authored fields matched. Name/context matches rank ahead of long-form story
matches; an empty search preserves the existing chronological Event order.

The Family History window also supports inclusive From/To date filters alongside
the existing year navigator. Search/filtering happens in memory over a background-
built Event index, so typing never reruns Timeline chronology or queries the DB per
keystroke. Narrative matches remain discovery-only and cannot affect chronology,
reconstruction, Event membership, EXIF, metadata observations, or source bytes.

CLI: `python -m ppa.cli event-search <library_id> "query"` with optional `--year`,
`--from`, `--to`, and `--json`.
See `docs/PHASE8_11_EVENT_SEARCH_DISCOVERY.md`.

## Phase 8.12 — Saved Event Views & Discovery Facets

Family History now exposes deterministic occasion, remembered-place, and people/group
facets alongside text/year/date search. Any current combination can be saved under a
human name such as **Christmas Events**, **Sydney**, or **Mum & Dad** and restored later.
A saved view stores the query/filter recipe only—not matching Event IDs—so it always
re-evaluates against current Events and remains discovery metadata rather than archive
truth. Saved views are Library-owned and schema v17 adds `saved_event_views`.

CLI: `python -m ppa.cli event-views list|save|run|delete ...`; Phase 8.11
`event-search` also accepts `--occasion`, `--place`, and `--person`.
See `docs/PHASE8_12_SAVED_EVENT_VIEWS.md`.

## Phase 8.13 — Favourites, Recently Viewed & Continue Reading

Phase 8.13 adds lightweight personal navigation state around durable human Events.
This state is intentionally **presentation-only** and never participates in
chronology, Event membership, reconstruction, anchors, metadata, or source-photo
writes.

### Family History

Family History now includes:

- **All Events**
- **★ Favourites**
- **Recently Viewed**
- **Continue where I left off…**

Opening an Event Story records a recent-view timestamp and increments a view
counter. The most recently opened Event becomes the Library's Continue target.
Recent navigation is bounded to the newest 100 Events per Library; Favourites
remain durable until explicitly removed.

Inside Story View, **☆ Favourite / ★ Favourite** toggles the preference without
changing the Event itself.

### CLI

```text
python -m ppa.cli event-activity favorites 1
python -m ppa.cli event-activity recent 1 --limit 20
python -m ppa.cli event-activity favorite <event-uuid>
python -m ppa.cli event-activity favorite <event-uuid> --off
```

### Schema v18

`event_navigation_state` stores only Event navigation preferences/history:

- favourite flag
- last viewed timestamp
- view count

SQLite triggers enforce Event/Library ownership. Deleting an Event cascades its
navigation state. No photographic evidence or chronology authority is stored in
this table.

## Phase 8.14 — Event Curation Health & Attention Indicators

Phase 8.14 adds a read-only Event-health projection over the existing Event,
Story Context, presentation preference, and Timeline state.  It does not create
new chronology or Event semantics.

Family History can now surface deterministic indicators including:

- Curation complete
- Has story / Needs story
- Custom cover / Custom order
- Contains ranges
- Contains tentative photos
- Contains unresolved photos
- Contains stale chronology
- Members outside the current Timeline scope
- Needs chronology review

`Curation complete` is deliberately narrow: the Event has human narrative
(description or story), all members are visible in the current Timeline
projection, and none are tentative, unplaced, or stale.  Custom presentation
preferences are optional and are not required for completion.

Family History adds `Needs Attention` and `Curation Complete` browse filters.
The same read model is available from the CLI:

```text
python -m ppa.cli event-health <library-id>
python -m ppa.cli event-health <library-id> --json event-health.json
```

Schema: `ppa-event-health/1`.

These indicators are presentation/curation guidance only.  They never alter
Timeline lanes, date reliability, reconstruction state, anchors, Event
membership, EXIF, or source photos.

## Phase 9.0 — Albums & Tags Foundation

Phase 9 begins the archive's non-chronological organisation layer. Albums and
Tags are durable, Library-owned, human-authored catalogue state attached to
logical Photos rather than physical File copies. Schema v19 adds audited Album
membership and Tag assignment with database-level cross-Library guards. These
labels are never chronology evidence and never write source photos.

CLI examples:

```text
python -m ppa.cli organize album-create 1 "Family Favourites"
python -m ppa.cli organize albums 1
python -m ppa.cli organize album-add <album-id> <photo-id>
python -m ppa.cli organize tag-create 1 Family
python -m ppa.cli organize tag-add <tag-id> <photo-id>
python -m ppa.cli organize tags 1
```

See `docs/PHASE9_0_ALBUMS_TAGS_FOUNDATION.md`.

### Phase 9.1 — Album & Tag Desktop Curation

The desktop now supports extended photo selection and an **Albums & Tags…** curation dialog. Bulk membership changes are atomic, use logical Photo identity, retain Library ownership guards, and append the same organisation audit history introduced in Phase 9.0. The photo inspector also shows current Album/Tag membership. See `docs/PHASE9_1_DESKTOP_CURATION.md`.

## Phase 9.2 — Album & Tag Browsing Views

Albums and Tags now open as first-class, read-only thumbnail views. Each logical Photo appears once even when multiple physical copies exist; a stable representative File is chosen only for rendering/Preview. Missing-only curated members remain visible, browsing is bounded/paged, and filename filtering searches all known filenames for that Photo in the Library. No chronology/evidence authority is introduced. See `docs/PHASE9_2_ALBUM_TAG_BROWSING.md`.

## Phase 9.3 — Album Curation & Presentation

Albums now support a human-selected logical-Photo cover and an explicit presentation order. Both are display-only preferences, append-audited, and automatically invalidated when Album membership changes in a way that makes them stale. See `docs/PHASE9_3_ALBUM_PRESENTATION.md`.

### Phase 9.4 — Album Home / Visual Album Library

The desktop now includes an **Albums** landing page with paged visual Album cards, preferred-cover thumbnails, descriptions, logical-photo/presence counts, search, and one-click entry into the existing Album browser. The projection is read-only (`ppa-album-home/1`) and remains orthogonal to chronology/evidence.

### Phase 9.5 — Tag Home & Organisational Discovery

Tags now have a first-class visual landing page with bounded card paging, deterministic cover representatives, explicit present/missing counts, name search, and direct browsing. Selecting two or more Tags creates an exact logical-Photo set intersection (for example `Family + Beach`) and opens it through the existing bounded organisation browser. Intersections are read-only, same-Library only, and never influence chronology or evidence.

### Phase 9.6 — Unified Organisation Discovery

The desktop now includes **Discover**, a combined Album + Tag query surface.
Any selected Albums and Tags are evaluated as an exact intersection over
logical Photo IDs, e.g. `Album: Holidays ∩ Tag: Beach ∩ Tag: Family`.
Selector indexes and result calculation both run off the GUI thread, while the
result reuses the existing bounded organisation browser. The structured result
schema is `ppa-organization-discovery/1`; no schema migration is required.
See `docs/PHASE9_6_UNIFIED_ORGANIZATION_DISCOVERY.md`.

### Phase 9.7 — Saved Organisation Views

Unified Album/Tag discovery recipes can now be named and reopened. A saved
view persists only the selected Album and Tag IDs; it never stores matching
Photo IDs. Reopening the view reevaluates the exact intersection against the
current organisation state. Saved views are Library-owned, case-insensitively
unique by name, cross-Library selectors fail closed, and deleting a saved view
does not alter Albums, Tags, membership, chronology, Events, metadata, or
source photographs.

CLI: `organization-views list|save|run|delete`.

## Phase 9.8 — Organisation Health & Curation Gaps

Adds a read-only organisation-quality projection for unorganised Photos, no-Album/no-Tag gaps, empty Albums, unused Tags, missing-only memberships, and broken saved discovery recipes. The desktop **Organisation Health** surface can open photo-level gaps in the existing logical-Photo browser. No organisation-health indicator is evidence or chronology authority.

### Phase 9.9 — Assisted Organisation Suggestions

PPA can now surface conservative, review-only Tag-gap suggestions from existing
human curation. If an Album or named Event has at least 5 logical Photos and an
existing Tag covers at least 80% of them with at least 4 explicit examples, PPA
may offer the untagged remainder for review. Nothing is auto-applied: accepted
suggestions are revalidated for freshness and then use the normal audited Tag
membership API. No chronology/evidence/source-photo authority is introduced.

CLI example:

```text
python -m ppa.cli organization-suggestions 1
python -m ppa.cli organization-suggestions 1 --json suggestions.json
```

Schema: `ppa-organization-suggestions/1`.

### Phase 9.10 — Suggestion Review History & Dismissal

Assisted Organisation now remembers explicit human review decisions. A dismissed recommendation is suppressed only while its exact peer/support fingerprint remains unchanged; changing the Album/Event peer composition produces a new fingerprint that may surface again. Accepted suggestions and dismiss/restore actions are audited separately from Album/Tag truth. Schema v22.

### Phase 9.11 — Organisation Activity & Change History

The desktop **Organisation Activity** view exposes recent Album/Tag curation changes from the append-only audit ledger. Direct membership adds/removals can be undone only when the current state still proves the exact inverse is safe; stale or ambiguous history remains review-only. Undo itself is append-audited and never rewrites prior history.

### Phase 9.12 — Shareable Organisation Report

Use **Export Organisation Report…** to create a sanitized ZIP summarising Albums,
Tags, organisation health, saved discovery views, assisted-suggestion review
state, and recent curation activity. Source paths, IDs, hashes, thumbnails,
database details and source-photo content are deliberately excluded.

CLI: `python -m ppa.cli organization-report <library_id> organisation-report.zip`

### Phase 9.12.1 — Pre-Freeze Adversarial Hardening

Before Phase 9 sign-off, the organisation stack received a focused adversarial hardening pass. Safe Undo and suggestion Apply/Dismiss now revalidate freshness inside `BEGIN IMMEDIATE` write transactions, closing TOCTOU windows. Organisation Activity and unified discovery use bounded query patterns rather than per-row/per-selector SELECTs. Shareable Organisation Reports also scrub private paths and identifier/hash-like material embedded inside human-authored text, and recent activity no longer exposes shortened logical Photo IDs. No schema migration or new authority is introduced. See `docs/PHASE9_12_1_PRE_FREEZE_HARDENING.md`.

## Phase 10.0 — Duplicate Identity & Copy-Lineage Foundation

Phase 10.0 makes exact-copy identity explicit and introduces human-confirmed lineage between
distinct logical Photos. Exact duplicates remain multiple Files under one Photo; derivative
relationships are separate directed edges with cycle protection and append-only history.
See `docs/PHASE10_0_DUPLICATE_LINEAGE_FOUNDATION.md`.

### Phase 10.1 — Duplicate & Lineage Review UI

The desktop now includes **Duplicates & Lineage**, with separate review lanes for current
SHA-confirmed exact physical copies, logical-photo identity divergences, and explicit
human Photo lineage. Exact or divergent physical files can be viewed side-by-side;
lineage can be added/removed only through the Phase-10 audited APIs. The UI never
auto-merges, auto-splits, deletes, selects a canonical winner, or infers lineage.
See `docs/PHASE10_1_DUPLICATE_LINEAGE_REVIEW_UI.md`.

### Phase 10.2 — Identity Divergence Investigation
Divergent logical Photos can now be investigated against immutable FileRevision history. PPA distinguishes proven modified-in-place cases from files that were merely distinct when first observed, while withholding explanation when history is incomplete. Investigation is read-only and never auto-splits/merges identity or creates lineage.

### Phase 10.3 — Controlled Identity Resolution

Identity divergence can now be resolved by a human-reviewed split of one complete current-SHA cohort into a new logical Photo. The split is revalidated under `BEGIN IMMEDIATE`, append-audited, refuses partial/cross-Library/competing identity cohorts, and never changes source bytes, EXIF, chronology, Events, Albums, Tags or FileRevision evidence. See `docs/PHASE10_3_CONTROLLED_IDENTITY_RESOLUTION.md`.

### Phase 10.4 — Identity Resolution Review & Recovery
Controlled split history now has a visual topology review and a fail-closed recovery path. Recovery reverses one exact audited split only when no later identity-dependent curation makes recombination ambiguous; freshness is re-proven inside `BEGIN IMMEDIATE`, and recovery appends a v25 audit record without deleting the original split history.

### Phase 10.5 — Identity Health & Resolution Queue
Adds a read-only priority queue for competing byte-identical logical Photos, current identity divergence, recoverable/review-only split history, and completed recombinations. No automatic identity correction is performed.

### Phase 10.6 — Competing Identity Investigation

P0 competing-identity cases now have a read-only forensic investigation that shows each logical Photo, every physical File, immutable revision history, first/last observation, and whether PPA observed previously different bytes converge to the shared current SHA. The investigation can only mark a narrowly clean two-Photo case as a candidate for a future controlled merge; Album/Tag history, lineage history, prior identity resolution, cross-Library identity, unknown hashes, or other current bytes force review-only status. No merge or source mutation is performed.

### Phase 10.7 — Controlled Identity Merge

Eligible Phase-10.6 competing logical Photos can now be merged only after explicit survivor selection. The complete File/revision state is fingerprinted and revalidated under `BEGIN IMMEDIATE`; the losing Photo identity is retired only after all its physical File records are atomically reassigned. Human Photo notes, organisation history, lineage history and prior identity-resolution/merge history block the operation. Source files remain untouched.

### Phase 11.0 — compact workspace navigation
The former 20+ item horizontal toolbar is now grouped into Library, Timeline,
Organisation, Identity, and Diagnostics workspace menus. Existing feature
actions and handlers are preserved; this is a navigation refactor with no
schema or authority change.

## Phase 11.0.1 — Workspace navigation dispatch hardening

Phase 11.0.1 hardens the compact workspace toolbar introduced in Phase 11.0.
Workspace menus now use menu-local proxy actions that explicitly dispatch the
existing canonical QAction commands rather than sharing those command actions
directly with QMenu.  Command enabled state remains authoritative and is
mirrored by the proxies.  A Qt regression now triggers representative entries
from every workspace and proves that the canonical command action actually
fires, closing the gap where Phase 11.0 only verified menu presence/labels.

## Phase 11.1 — Command Palette & Keyboard Navigation

- `Ctrl+Shift+P` searchable command launcher over canonical application actions.
- Compact **Commands…** toolbar entry for discoverability.
- `Alt+1` … `Alt+5` opens Library, Timeline, Organisation, Identity, and Diagnostics workspaces.
- Disabled commands remain visible but cannot be executed through the palette.
- No alternate handler path: palette execution calls the existing canonical `QAction`.


### Phase 11.1.1 — Command Palette Label Fix

Preserve canonical QAction labels in the command palette. Literal ampersands in commands such as `Albums & Tags…` and `Duplicates & Lineage` are no longer stripped during palette indexing/display. The Windows Qt regression now verifies the exact canonical labels before dispatch/state checks.

### Phase 11.2 — Navigation Polish & Usability

Workspace commands now expose concise descriptions in menus and the command palette. Palette search includes those descriptions, selected commands show purpose/availability, and the five most recently launched palette commands are recalled for the current application session. Recent-command recall is intentionally non-persistent and does not touch archive or source state. See `docs/PHASE11_2_NAVIGATION_POLISH.md`.

## Phase 11.2.1 — Command Palette Search Ranking Fix

Phase 11.2 expanded palette matching to command descriptions. That correctly allows multiple relevant results for a query such as `organisation health`, but the older Phase 11.1 Qt smoke test still required exactly one result.

11.2.1 formalises deterministic ranking instead:
- exact command-label match first;
- label-token matches next;
- workspace + label matches next;
- description-dependent matches remain discoverable afterward;
- original command registry order breaks ties deterministically.

No archive data, schema, command dispatch, recent-command state, or source-photo behavior changes.

## Phase 12.0 — Backup & Archive Health Foundation

The **Library → Archive Health** surface adds a read-only view of catalogue copy coverage: no-present, single-present, multiple exact present, partially missing, unhealthy, unknown-hash, and current-content-divergence indicators. Multiple exact Files are deliberately **not** described as independent backups because Phase 12.0 has not yet captured storage-device or hard-link identity. CLI: `python -m ppa.cli archive-health <library-id> [--json archive-health.json]`. See `docs/PHASE12_0_ARCHIVE_HEALTH.md`.

## Phase 12.1 — Filesystem Storage Identity & Hard-Link Awareness

Normal library scans now retain the filesystem `device + object/file-index + link-count` evidence already available from read-only `stat()` calls. Archive Health schema `ppa-archive-health/2` uses that evidence to distinguish **hard-linked paths** from **distinct filesystem objects**, and separately reports exact-copy sets spanning distinct filesystem device IDs. Existing catalogue rows upgrade with unknown storage identity until their next normal scan; no migration reads or mutates source photos. Distinct object/device evidence is deliberately **not** described as proof of independent physical backup hardware or failure domains. See `docs/PHASE12_1_STORAGE_IDENTITY.md`.

