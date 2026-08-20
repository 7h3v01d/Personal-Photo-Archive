# Personal Photo Archive

Local-first digital photography management and preservation platform.
See `docs/ARCHIVE_SAFETY_CONTRACT.md` for the non-negotiable rules every
feature is built against.

Status: **Archive Core Hardening ACCEPTED (Schema v6). Phase 6 Slice 1 FROZEN; Slice 2.1 (cross-photo/sequence evidence) — camera-aware, segmented; reset patterns flagged not escalated; order conflicts doubt both sides. 139 tests, 0 xfails.**

The Library -> File -> FileRevision -> Observation model holds under attack;
this slice closed the transaction/identity edges: a failed scan now rolls
back ALL partial reconciliation before recording FAILED (no half-committed
catalogue, no orphaned revision); within-library identity is the canonical
relative path (a respelled/junctioned root is no longer a phantom move);
overlapping library roots are rejected; and metadata extraction records its
extractor name/version so a version bump re-runs extraction. Catalogue reads
now use presence_status; `status`/`files.sha256` remain maintained compat
mirrors.

See `docs/HARDENING.md` for the full findings -> fix -> test map (25 across
Hardening 1-3.2). Per the reviewer, Phase 6 (Date Reliability Engine) can
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
- open the desktop window: **Add Library…**, **Scan**, **Verify**,
  **Extract Metadata**, and a grid-size control; a thumbnail grid
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
