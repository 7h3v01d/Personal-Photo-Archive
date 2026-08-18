# Personal Photo Archive

Local-first digital photography management and preservation platform.
See `docs/ARCHIVE_SAFETY_CONTRACT.md` for the non-negotiable rules every
feature is built against.

Current status: **Desktop UI (Phase 4/5 vertical slice) on the Phase 2 engine.**
(Phases 0-2 complete. Phase 3 — Metadata Extraction — is the next backend gap.)

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
- open the desktop window: **Add Library…**, **Scan**, **Verify**, a
  thumbnail grid (All / Recently Added / Duplicates / Missing), and an
  inspector showing each file's identity, dimensions, SHA-256, copy count,
  and integrity-event history

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
    thumbnails.py         SHA-256-keyed thumbnail cache (no Qt)
    ui/
        theme.py         Dark industrial palette + QSS
        workers.py       Scan/verify/thumbnail workers (QObject on QThread)
        models.py        Thumbnail grid model (lazy loading)
        main_window.py   Nav / grid / inspector window
    db/
        schema.sql       SQLite schema v1
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

## Next up (per the roadmap)

Phase 3 — Metadata Extraction: read EXIF/filesystem/filename metadata into
`metadata_observations` as **observations** (source + value + timestamp),
never as unattributed truth. The table already exists and is key/value, so
no migration is needed. This is what fills in the currently-empty camera,
capture-date, GPS, and exposure fields the inspector is ready to display.
Interpreted dates and the confidence/evidence model stay out until Phase
6/7, so they aren't designed against guesses.
