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

    chron_parser = subparsers.add_parser(
        "chronology",
        help="Cross-photo/sequence date analysis: reset-clock runs, regression "
             "(read-only)",
    )
    chron_parser.add_argument(
        "--min-reset-run", type=int, default=None,
        help="Minimum sequential-file run at a reset epoch to call a clock reset",
    )

    rec_parser = subparsers.add_parser(
        "reconcile",
        help="Full date assessment incl. independent calendar evidence "
             "(anchors/GPS/manufacture floors) (read-only)",
    )
    rec_parser.add_argument(
        "--floors", default=None,
        help="Path to a camera manufacture-floor JSON file",
    )
    rec_parser.add_argument("--rating", default=None, help="Only show this rating")
    rec_parser.add_argument("--export", default=None,
                            help="Write the full assessment to this CSV path")

    anchor_parser = subparsers.add_parser(
        "anchor", help="Manage user anchors (human-asserted dates)")
    anchor_sub = anchor_parser.add_subparsers(dest="anchor_command", required=True)
    a_add = anchor_sub.add_parser("add", help="Add an anchor")
    a_add.add_argument("scope", choices=["file", "directory", "library"])
    a_add.add_argument("scope_ref", help="file id / directory path / library id")
    a_add.add_argument("start_date", help="YYYY-MM-DD")
    a_add.add_argument("--end-date", default=None, help="YYYY-MM-DD (makes it a range)")
    a_add.add_argument("--note", default=None)
    a_add.add_argument("--library-id", type=int, default=None,
                       help="Owning library id (auto for file/library scope; set "
                            "this for directory anchors so removal can clean them)")
    anchor_sub.add_parser("list", help="List anchors")

    rc_parser = subparsers.add_parser(
        "reconstruct",
        help="Historical date reconstruction: propose/confirm/reject interpreted "
             "capture dates (never overwrites the recorded date)")
    rc_sub = rc_parser.add_subparsers(dest="reconstruct_command", required=True)
    rc_run = rc_sub.add_parser("run", help="Compute and store reconstruction proposals")
    rc_run.add_argument("--floors", default=None, help="Camera manufacture-floor JSON")
    rc_list = rc_sub.add_parser("list", help="List stored reconstructions")
    rc_list.add_argument("--status", default=None,
                         choices=["proposed", "confirmed", "rejected"])
    rc_confirm = rc_sub.add_parser("confirm", help="Confirm a file's reconstruction")
    rc_confirm.add_argument("file_id")
    rc_reject = rc_sub.add_parser("reject", help="Reject a file's reconstruction")
    rc_reject.add_argument("file_id")
    rc_reopen = rc_sub.add_parser(
        "reopen", help="Return a confirmed/rejected reconstruction to proposed")
    rc_reopen.add_argument("file_id")

    pilot_parser = subparsers.add_parser(
        "pilot", help="Read-only collection-level pilot analysis report")
    pilot_sub = pilot_parser.add_subparsers(dest="pilot_command", required=True)
    pilot_report = pilot_sub.add_parser("report", help="Analyse one library/subset")
    pilot_report.add_argument("library_id", type=int, help="Library id to analyse")
    pilot_report.add_argument("--directory", default=None,
                              help="Relative directory prefix within the library")
    pilot_report.add_argument("--json", dest="json_path", default=None,
                              help="Write the structured report JSON to this path")

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

    if args.command == "chronology":
        from ppa.chronology import analyse_library

        kwargs = {}
        if args.min_reset_run is not None:
            kwargs["min_reset_run"] = args.min_reset_run
        findings, chron = analyse_library(conn, **kwargs)

        resets = [f for f in findings if f.kind == "reset_pattern"]
        regs = [f for f in findings if f.kind == "timestamp_order_conflict"]
        print(f"Cross-photo chronology: {len(resets)} reset-clock pattern(s), "
              f"{len(regs)} order conflict(s).\n")
        for f in resets:
            print(f"  RESET PATTERN ({len(f.file_ids)} files): {f.detail}")
        for f in regs:
            print(f"  ORDER CONFLICT: {f.detail}")

        downgraded = sum(1 for c in chron.values()
                         if c.reliability.value != c.intrinsic.value)
        print(f"\n{downgraded} photo(s) re-rated (downgraded) by cross-photo evidence.")
        print("Reset patterns are flagged for investigation but NOT concluded wrong "
              "without independent calendar evidence.")
        print("(Read-only; intrinsic assessments and stored dates are unchanged.)")
        return 0

    if args.command == "anchor":
        from ppa import anchors as anchors_mod
        if args.anchor_command == "add":
            kind = "range" if args.end_date else "exact"
            aid = anchors_mod.add_anchor(conn, args.scope, args.scope_ref, kind,
                                         args.start_date, args.end_date, args.note,
                                         args.library_id)
            print(f"Added anchor #{aid} ({args.scope} {args.scope_ref}, {kind}).")
            return 0
        if args.anchor_command == "list":
            for a in anchors_mod.list_anchors(conn):
                span = a.start_date if a.end_date is None else f"{a.start_date}…{a.end_date}"
                note = f"  — {a.note}" if a.note else ""
                print(f"  #{a.id} {a.scope}:{a.scope_ref}  {a.kind} {span}{note}")
            return 0
        return 1

    if args.command == "reconcile":
        from collections import Counter
        from ppa.reconcile import analyse_library_reconciled
        from ppa.camera_floors import CameraFloors

        floors = CameraFloors.load(args.floors) if args.floors else None
        if args.export:
            from ppa.reconcile import export_reconciliation_csv
            n = export_reconciliation_csv(conn, args.export, camera_floors=floors)
            print(f"Wrote {n} photo assessment(s) to {args.export} (read-only).")
            return 0
        findings, results = analyse_library_reconciled(conn, camera_floors=floors)

        want = args.rating.upper() if args.rating else None
        counts: Counter[str] = Counter()
        for fid, fa in sorted(results.items()):
            counts[fa.reliability.value] += 1
        changed = [fa for fa in results.values() if fa.changed]
        for fa in changed:
            if want and fa.reliability.value != want:
                continue
            date = fa.date.date().isoformat() if fa.date else "-"
            print(f"  {fa.reliability.value:15} {date:12} {fa.file_id[:12]}"
                  f"  {'; '.join(fa.reasons[:1])}")
            for c in fa.evidence_conflicts:
                print(f"      conflict: {c}")
        print("\nSummary (after independent calendar evidence):")
        for rating in ("TRUSTED", "PROBABLY_VALID", "QUESTIONABLE",
                       "LIKELY_WRONG", "UNKNOWN"):
            if counts.get(rating):
                print(f"  {rating:15} {counts[rating]}")
        print(f"\n{len(changed)} photo(s) re-rated by independent calendar evidence.")
        print("(Read-only; no photo, observation, or stored date was modified.)")
        return 0

    if args.command == "pilot":
        from ppa.pilot import analyse_pilot, concise_text
        if args.pilot_command == "report":
            try:
                report = analyse_pilot(conn, library_id=args.library_id,
                                       directory_prefix=args.directory)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(concise_text(report))
            if args.json_path:
                Path(args.json_path).write_text(report.to_json() + "\n", encoding="utf-8")
                print(f"\nWrote {args.json_path}")
            return 0
        return 1

    if args.command == "reconstruct":
        from ppa import reconstruct_catalogue as rc
        if args.reconstruct_command == "run":
            from ppa.camera_floors import CameraFloors
            floors = CameraFloors.load(args.floors) if args.floors else None
            counts = rc.store_reconstructions(conn, camera_floors=floors)
            print(f"Reconstruction proposals: {counts['proposed']} written, "
                  f"{counts['skipped_decided']} left as decided, "
                  f"{counts['cleared']} stale cleared.")
            print("(Interpretation only; the recorded date and observations are "
                  "unchanged.)")
            return 0
        if args.reconstruct_command == "list":
            rows = rc.list_reconstructions(conn, status=args.status)
            for r in rows:
                span = r.start_date if r.end_date is None else f"{r.start_date}…{r.end_date}"
                if r.content_stale and r.evidence_stale:
                    flag = " STALE(bytes+evidence)"
                elif r.content_stale:
                    flag = " STALE(bytes)"
                elif r.evidence_stale:
                    flag = " STALE(evidence)"
                else:
                    flag = ""
                print(f"  {r.status:9} {r.confidence:9} {span:23} {r.method:12} "
                      f"{r.file_id[:12]}{flag}  {r.evidence or ''}")
            print(f"\n{len(rows)} reconstruction(s).")
            return 0
        if args.reconstruct_command in ("confirm", "reject"):
            fn = rc.confirm_reconstruction if args.reconstruct_command == "confirm" \
                else rc.reject_reconstruction
            try:
                ok = fn(conn, args.file_id)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print("Done." if ok else "No reconstruction for that file.")
            return 0 if ok else 1
        if args.reconstruct_command == "reopen":
            ok = rc.reopen_reconstruction(conn, args.file_id)
            print("Reopened (re-run reconstruct to refresh)." if ok
                  else "No decided reconstruction to reopen.")
            return 0 if ok else 1
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
