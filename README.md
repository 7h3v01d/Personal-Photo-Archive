# Personal Photo Archive

Local-first digital photography management and preservation platform.
See `docs/ARCHIVE_SAFETY_CONTRACT.md` for the non-negotiable rules every
feature is built against.

Current status: **Phase 0 — Project Foundation.**

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
    db/
        schema.sql       SQLite schema v1
        connection.py    DB open/init
tests/                  pytest suite
docs/
    ARCHIVE_SAFETY_CONTRACT.md
data/sample_library/    Real (small, personal) photo subset for dev/testing —
                         not committed to git, see .gitignore
```

## Next up (per the roadmap)

Phase 1 — Safe Library Scanner: recursive scan of a real library directory,
capturing file-level facts (size, mtime, dimensions, type) without touching
anything, differentiating new/known/moved/renamed/modified/missing files.
