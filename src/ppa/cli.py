"""Command-line entry point.

Useful for running a scan against a real sample library directory without
booting the Qt application — the fastest way to see what your actual
collection looks like to the scanner.

Usage:
    python -m ppa.cli scan /path/to/sample_library
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ppa.config import Config
from ppa.db import connect
from ppa.logging_setup import configure_logging, get_logger
from ppa.scanner import scan_library


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ppa")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan a library directory")
    scan_parser.add_argument("path", type=Path, help="Directory to scan")

    args = parser.parse_args(argv)

    config = Config.load()
    configure_logging(config.log_path, config.log_level)
    log = get_logger("cli")

    if args.command == "scan":
        if not args.path.is_dir():
            print(f"Not a directory: {args.path}", file=sys.stderr)
            return 1

        conn = connect(config.db_path)
        log.info("Scanning %s", args.path)
        report = scan_library(conn, args.path)
        print(report.summary())

        if report.inaccessible_files:
            print("\nInaccessible files:")
            for path, reason in report.inaccessible_files:
                print(f"  {path}: {reason}")

        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
