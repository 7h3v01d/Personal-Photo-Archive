"""Command-line entry point.

Useful for running a scan or an integrity check against a real library
directory without booting the Qt application — the fastest way to see what
your actual collection looks like to the archive.

Usage:
    python -m ppa.cli scan   /path/to/library
    python -m ppa.cli verify
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ppa.config import Config
from ppa.db import connect
from ppa.integrity import verify_library
from ppa.logging_setup import configure_logging, get_logger
from ppa.metadata import extract_stale
from ppa.scanner import scan_library


def _protected_paths(config: Config) -> list[Path]:
    """Operational paths that must never live inside a scanned library."""
    data_dir = config.db_path.parent
    return [config.db_path, data_dir / "thumbnails", config.log_path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ppa")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan a library directory")
    scan_parser.add_argument("path", type=Path, help="Directory to scan")

    subparsers.add_parser(
        "verify",
        help="Re-hash catalogued files to detect silent corruption",
    )

    subparsers.add_parser(
        "extract",
        help="Read EXIF/metadata into the catalogue as observations",
    )

    dates_parser = subparsers.add_parser(
        "dates",
        help="Assess timestamp reliability of catalogued photos (read-only)",
    )
    dates_parser.add_argument(
        "--rating", default=None,
        help="Only show photos with this rating "
             "(TRUSTED/PROBABLY_VALID/QUESTIONABLE/LIKELY_WRONG/UNKNOWN)",
    )

    args = parser.parse_args(argv)

    config = Config.load()
    configure_logging(config.log_path, config.log_level)
    log = get_logger("cli")

    conn = connect(config.db_path)

    if args.command == "scan":
        if not args.path.is_dir():
            print(f"Not a directory: {args.path}", file=sys.stderr)
            return 1

        log.info("Scanning %s", args.path)
        protected = _protected_paths(config)
        report = scan_library(conn, args.path, protected_paths=protected)
        print(report.summary())

        if report.inaccessible_files:
            print("\nInaccessible files:")
            for path, reason in report.inaccessible_files:
                print(f"  {path}: {reason}")
        print("\nRun 'ppa extract' to read EXIF metadata into the catalogue.")
        return 0

    if args.command == "extract":
        log.info("Extracting metadata")
        count = extract_stale(conn, progress_cb=lambda m: print(m, end="\r"))
        print(f"\nMetadata read for {count} file(s).")
        return 0

    if args.command == "verify":
        log.info("Verifying catalogue integrity")
        report = verify_library(conn)
        print(report.summary())

        if report.problems:
            print("\nProblems found:")
            for path, reason in report.problems:
                print(f"  {path}: {reason}")
        return 0

    if args.command == "dates":
        from collections import Counter
        from ppa.dating import assess_file

        rows = conn.execute(
            "SELECT id, filename FROM files WHERE presence_status = 'present' "
            "ORDER BY filename"
        ).fetchall()
        counts: Counter[str] = Counter()
        want = args.rating.upper() if args.rating else None
        for r in rows:
            a = assess_file(conn, r["id"])
            counts[a.reliability.value] += 1
            if want and a.reliability.value != want:
                continue
            est = a.candidate_date.date().isoformat() if a.candidate_date else "-"
            print(f"  {a.reliability.value:15} {est:12} {r['filename']}")
        print("\nSummary:")
        for rating in ("TRUSTED", "PROBABLY_VALID", "QUESTIONABLE",
                       "LIKELY_WRONG", "UNKNOWN"):
            if counts.get(rating):
                print(f"  {rating:15} {counts[rating]}")
        print("\n(Read-only assessment; no photo or date was modified.)")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
