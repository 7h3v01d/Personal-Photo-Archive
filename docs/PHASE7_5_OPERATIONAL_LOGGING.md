# Phase 7.5 — Operational Logging & Exportable Diagnostics

Phase 7.5 makes PPA's operational activity observable without turning logs into archive evidence.

## Runtime files

The configured logging directory now contains two rotating logs:

- `ppa.log` — human-readable activity log for live monitoring.
- `ppa.jsonl` — structured JSON Lines records (`timestamp`, `level`, `logger`, `message`, optional exception).

Each rotates at 5 MiB with five retained backups.

## Desktop workflow

The main toolbar provides:

- **Activity Log…** — live auto-refreshing tail of `ppa.log`, with a shortcut to the log folder.
- **Export Diagnostics…** — writes a sanitized ZIP suitable for sharing during debugging.

The log window also contains **Export diagnostics…**.

## CLI

```text
python -m ppa.cli diagnostics tail
python -m ppa.cli diagnostics tail --lines 300
python -m ppa.cli diagnostics export ppa-diagnostics.zip
```

## Export safety contract

The diagnostics ZIP contains sanitized operational logs plus a manifest. It intentionally excludes:

- catalogue database;
- source photographs;
- thumbnails;
- pilot session artifacts;
- raw configuration paths.

Known home/data/library roots are replaced with tokens such as `<HOME>`, `<PPA_DATA>`, and `<LIBRARY_1>` before export. The local live logs remain unredacted because absolute paths are useful for local diagnosis; only the explicit export is treated as shareable.

Operational logging never changes EXIF, source bytes, metadata observations, anchors, reconstructions, or decisions.
