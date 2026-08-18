# Personal Photo Archive

Local-first digital photography management and preservation platform.
See `docs/ARCHIVE_SAFETY_CONTRACT.md` for the non-negotiable rules every
feature is built against.

Current status: **Phase 1 — Safe Library Scanner.**

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
- open a mostly-blank window confirming the plumbing works

## Scan a library (no GUI needed)

```bash
python -m ppa.cli scan /path/to/your/sample_library
```

This is the fastest way to run the scanner against a real slice of your
collection and see what it reports before any UI exists. It only ever
reads from the directory you point it at — see
`docs/ARCHIVE_SAFETY_CONTRACT.md`.

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
    scanner.py            Phase 1 safe library scanner
    db/
        schema.sql       SQLite schema v1
        connection.py    DB open/init
tests/                  pytest suite
docs/
    ARCHIVE_SAFETY_CONTRACT.md
data/sample_library/    Real (small, personal) photo subset for dev/testing —
                         not committed to git, see .gitignore
```

## Scanner notes (read before trusting move/rename results)

Phase 1 has no content hash yet, so move/rename detection is a **heuristic**:
same filename + same size = "moved"; same size only = "possibly renamed".
Both are provisional and logged as such in `integrity_events` — Phase 2's
SHA-256 reconciliation is what actually confirms file identity. Nothing
from this heuristic is treated as ground truth downstream.

## Next up (per the roadmap)

Phase 2 — Cryptographic Identity and Integrity: SHA-256 hashing to turn
the Phase 1 move/rename heuristics into confirmed identity, first-seen
timestamps, and corruption warnings on re-verification.
