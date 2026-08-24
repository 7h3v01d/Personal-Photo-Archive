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

    timeline_parser = subparsers.add_parser(
        "timeline", help="Build the read-only provenance-aware chronology timeline")
    timeline_parser.add_argument("library_id", type=int, help="Library id to view")
    timeline_parser.add_argument("--directory", default=None,
                                 help="Relative directory prefix within the library")
    timeline_parser.add_argument("--json", dest="json_path", default=None,
                                 help="Write structured timeline JSON to this path")

    cluster_parser = subparsers.add_parser(
        "timeline-clusters", help="Detect provisional read-only chronological browsing clusters")
    cluster_parser.add_argument("library_id", type=int, help="Library id to view")
    cluster_parser.add_argument("--directory", default=None,
                                help="Relative directory prefix within the library")
    cluster_parser.add_argument("--json", dest="json_path", default=None,
                                help="Write structured cluster JSON to this path")

    story_parser = subparsers.add_parser(
        "event-story", help="Render one durable human Event as a read-only story view")
    story_parser.add_argument("event_id", help="Event UUID to render")
    story_parser.add_argument("--json", dest="json_path", default=None,
                              help="Write structured event-story JSON to this path")

    event_browse_parser = subparsers.add_parser(
        "event-browse", help="List durable human Events in deterministic story-reading order")
    event_browse_parser.add_argument("library_id", type=int, help="Library id to browse")
    event_browse_parser.add_argument("--json", dest="json_path", default=None,
                                     help="Write structured event-browse JSON to this path")

    event_home_parser = subparsers.add_parser(
        "event-home", help="Build the read-only Family History Event-card landing view")
    event_home_parser.add_argument("library_id", type=int, help="Library id to browse")
    event_home_parser.add_argument("--json", dest="json_path", default=None,
                                   help="Write structured Family History JSON to this path")

    album_home_parser = subparsers.add_parser(
        "album-home", help="Build the read-only visual Album-card landing view")
    album_home_parser.add_argument("library_id", type=int, help="Library id to browse")
    album_home_parser.add_argument("--json", dest="json_path", default=None,
                                   help="Write structured Album Home JSON to this path")


    tag_home_parser = subparsers.add_parser(
        "tag-home", help="Build the read-only visual Tag-card landing view")
    tag_home_parser.add_argument("library_id", type=int, help="Library id to browse")
    tag_home_parser.add_argument("--json", dest="json_path", default=None,
                                 help="Write structured Tag Home JSON to this path")

    tag_intersection_parser = subparsers.add_parser(
        "tag-intersection", help="Browse the explicit logical-Photo intersection of Tags")
    tag_intersection_parser.add_argument("library_id", type=int, help="Library id to browse")
    tag_intersection_parser.add_argument("tag_ids", nargs="+", help="Two or more Tag UUIDs")
    tag_intersection_parser.add_argument("--json", dest="json_path", default=None,
                                         help="Write structured intersection JSON to this path")

    event_health_parser = subparsers.add_parser(
        "event-health", help="Summarise read-only Event curation/chronology attention indicators")
    event_health_parser.add_argument("library_id", type=int, help="Library id to inspect")
    event_health_parser.add_argument("--json", dest="json_path", default=None,
                                     help="Write structured Event-health JSON to this path")

    event_search_parser = subparsers.add_parser(
        "event-search", help="Search human-authored Events and Story Context")
    event_search_parser.add_argument("library_id", type=int, help="Library id to search")
    event_search_parser.add_argument("query", nargs="?", default="", help="Search text; tokens use AND semantics")
    event_search_parser.add_argument("--year", type=int, default=None, help="Restrict to Events starting in this year")
    event_search_parser.add_argument("--from", dest="start_date", default=None, help="Inclusive Event-span start filter (YYYY-MM-DD)")
    event_search_parser.add_argument("--to", dest="end_date", default=None, help="Inclusive Event-span end filter (YYYY-MM-DD)")
    event_search_parser.add_argument("--occasion", default=None, help="Restrict to matching human occasion/context text")
    event_search_parser.add_argument("--place", default=None, help="Restrict to matching remembered place text")
    event_search_parser.add_argument("--person", default=None, help="Restrict to matching people notes")
    event_search_parser.add_argument("--json", dest="json_path", default=None,
                                     help="Write structured Event-search JSON to this path")

    event_activity_parser = subparsers.add_parser(
        "event-activity", help="Manage Event favourites and inspect recent Story navigation")
    event_activity_sub = event_activity_parser.add_subparsers(dest="event_activity_command", required=True)
    ea_fav = event_activity_sub.add_parser("favorite", help="Mark/unmark one Event as favourite")
    ea_fav.add_argument("event_id"); ea_fav.add_argument("--off", action="store_true")
    ea_recent = event_activity_sub.add_parser("recent", help="List recently viewed Events")
    ea_recent.add_argument("library_id", type=int); ea_recent.add_argument("--limit", type=int, default=20)
    ea_list = event_activity_sub.add_parser("favorites", help="List favourite Events")
    ea_list.add_argument("library_id", type=int)

    organize_parser = subparsers.add_parser(
        "organize", help="Manage Phase-9 logical-photo Albums and Tags")
    organize_sub = organize_parser.add_subparsers(dest="organize_command", required=True)
    org_albums = organize_sub.add_parser("albums", help="List Albums in one Library")
    org_albums.add_argument("library_id", type=int)
    org_album_create = organize_sub.add_parser("album-create", help="Create an Album")
    org_album_create.add_argument("library_id", type=int); org_album_create.add_argument("name")
    org_album_create.add_argument("--description", default=None)
    org_album_add = organize_sub.add_parser("album-add", help="Add a logical Photo to an Album")
    org_album_add.add_argument("album_id"); org_album_add.add_argument("photo_id")
    org_album_remove = organize_sub.add_parser("album-remove", help="Remove a logical Photo from an Album")
    org_album_remove.add_argument("album_id"); org_album_remove.add_argument("photo_id")
    org_tags = organize_sub.add_parser("tags", help="List Tags in one Library")
    org_tags.add_argument("library_id", type=int)
    org_tag_create = organize_sub.add_parser("tag-create", help="Create/reuse a Tag")
    org_tag_create.add_argument("library_id", type=int); org_tag_create.add_argument("name")
    org_tag_add = organize_sub.add_parser("tag-add", help="Apply a Tag to a logical Photo")
    org_tag_add.add_argument("tag_id"); org_tag_add.add_argument("photo_id")
    org_tag_remove = organize_sub.add_parser("tag-remove", help="Remove a Tag from a logical Photo")
    org_tag_remove.add_argument("tag_id"); org_tag_remove.add_argument("photo_id")

    event_views_parser = subparsers.add_parser(
        "event-views", help="Manage durable saved Family History discovery views")
    event_views_sub = event_views_parser.add_subparsers(dest="event_views_command", required=True)
    ev_list = event_views_sub.add_parser("list", help="List saved views for one library")
    ev_list.add_argument("library_id", type=int)
    ev_save = event_views_sub.add_parser("save", help="Create/update a named saved discovery view")
    ev_save.add_argument("library_id", type=int); ev_save.add_argument("name")
    ev_save.add_argument("--query", default=""); ev_save.add_argument("--year", type=int, default=None)
    ev_save.add_argument("--from", dest="start_date", default=None); ev_save.add_argument("--to", dest="end_date", default=None)
    ev_save.add_argument("--occasion", default=None); ev_save.add_argument("--place", default=None); ev_save.add_argument("--person", default=None)
    ev_delete = event_views_sub.add_parser("delete", help="Delete one saved view")
    ev_delete.add_argument("view_id")
    ev_run = event_views_sub.add_parser("run", help="Evaluate one saved view against current Events")
    ev_run.add_argument("view_id"); ev_run.add_argument("--json", dest="json_path", default=None)

    pilot_parser = subparsers.add_parser(
        "pilot", help="Read-only collection-level pilot analysis report")
    pilot_sub = pilot_parser.add_subparsers(dest="pilot_command", required=True)
    pilot_report = pilot_sub.add_parser("report", help="Analyse one library/subset")
    pilot_report.add_argument("library_id", type=int, help="Library id to analyse")
    pilot_report.add_argument("--directory", default=None,
                              help="Relative directory prefix within the library")
    pilot_report.add_argument("--json", dest="json_path", default=None,
                              help="Write the structured report JSON to this path")
    pilot_queue = pilot_sub.add_parser("queue", help="Build prioritised date-review queue")
    pilot_queue.add_argument("library_id", type=int, help="Library id to review")
    pilot_queue.add_argument("--directory", default=None,
                             help="Relative directory prefix within the library")
    pilot_queue.add_argument("--all", action="store_true",
                             help="Include Priority D / currently non-actionable files")
    pilot_queue.add_argument("--json", dest="json_path", default=None,
                             help="Write the structured queue JSON to this path")
    pilot_questions = pilot_sub.add_parser(
        "questions", help="Rank high-leverage human date questions")
    pilot_questions.add_argument("library_id", type=int, help="Library id to analyse")
    pilot_questions.add_argument("--directory", default=None,
                                 help="Relative directory prefix within the library")
    pilot_questions.add_argument("--json", dest="json_path", default=None,
                                 help="Write structured opportunities JSON to this path")
    pilot_explain = pilot_sub.add_parser(
        "explain", help="Explain the Phase-6/7 date evidence for one file (read-only)")
    pilot_explain.add_argument("file_id", help="Catalogued file id to explain")
    pilot_explain.add_argument("--json", dest="json_path", default=None,
                               help="Write structured evidence-trace JSON to this path")
    pilot_unresolved = pilot_sub.add_parser(
        "unresolved", help="Classify photos whose dates remain unresolved (read-only)")
    pilot_unresolved.add_argument("library_id", type=int, help="Library id to analyse")
    pilot_unresolved.add_argument("--directory", default=None,
                                  help="Relative directory prefix within the library")
    pilot_unresolved.add_argument("--json", dest="json_path", default=None,
                                  help="Write structured unresolved-memory JSON to this path")
    pilot_audit = pilot_sub.add_parser(
        "audit", help="Capture a read-only Phase-7 pilot audit snapshot")
    pilot_audit.add_argument("library_id", type=int, help="Library id to audit")
    pilot_audit.add_argument("--directory", default=None,
                             help="Relative directory prefix within the library")
    pilot_audit.add_argument("--json", dest="json_path", default=None,
                             help="Write structured audit snapshot JSON to this path")
    pilot_compare = pilot_sub.add_parser(
        "audit-compare", help="Compare two explicit pilot audit JSON snapshots")
    pilot_compare.add_argument("before", help="Earlier audit JSON path")
    pilot_compare.add_argument("after", help="Later audit JSON path")
    pilot_compare.add_argument("--json", dest="json_path", default=None,
                               help="Write structured comparison JSON to this path")
    pilot_session_start = pilot_sub.add_parser(
        "session-start", help="Start a durable real-collection pilot session")
    pilot_session_start.add_argument("library_id", type=int, help="Library id to pilot")
    pilot_session_start.add_argument("session_path", help="Pilot session JSON path")
    pilot_session_start.add_argument("--directory", default=None,
                                     help="Relative directory prefix within the library")
    pilot_session_checkpoint = pilot_sub.add_parser(
        "session-checkpoint", help="Append a current audit checkpoint to an open pilot session")
    pilot_session_checkpoint.add_argument("session_path", help="Pilot session JSON path")
    pilot_session_checkpoint.add_argument("--label", default=None, help="Optional checkpoint label")
    pilot_session_status = pilot_sub.add_parser(
        "session-status", help="Show a saved pilot session without changing it")
    pilot_session_status.add_argument("session_path", help="Pilot session JSON path")
    pilot_session_close = pilot_sub.add_parser(
        "session-close", help="Close a pilot session with a final audit and comparison")
    pilot_session_close.add_argument("session_path", help="Pilot session JSON path")
    pilot_session_report = pilot_sub.add_parser(
        "session-report", help="Export a shareable Phase-7 pilot progress ZIP")
    pilot_session_report.add_argument("session_path", help="Pilot session JSON path")
    pilot_session_report.add_argument("output", help="Destination ZIP path")

    diag_parser = subparsers.add_parser(
        "diagnostics", help="Monitor or export operational diagnostics")
    diag_sub = diag_parser.add_subparsers(dest="diagnostics_command", required=True)
    diag_tail = diag_sub.add_parser("tail", help="Show the latest human-readable log entries")
    diag_tail.add_argument("--lines", type=int, default=120, help="Number of log lines to show")
    diag_export = diag_sub.add_parser("export", help="Create a sanitized shareable diagnostics ZIP")
    diag_export.add_argument("path", help="Destination ZIP path")
    diag_runs = diag_sub.add_parser("runs", help="List recent correlated operational runs")
    diag_runs.add_argument("--limit", type=int, default=30, help="Maximum runs to show")
    diag_run_export = diag_sub.add_parser("run-export", help="Export one sanitized run transcript")
    diag_run_export.add_argument("run_id", help="Run id from 'diagnostics runs'")
    diag_run_export.add_argument("path", help="Destination JSON path")

    args = parser.parse_args(argv)

    config = Config.load()
    configure_logging(config.log_path, config.log_level)
    log = get_logger("cli")

    conn = connect(config.db_path)

    if args.command == "diagnostics":
        from ppa.diagnostics import export_diagnostics, tail_text
        if args.diagnostics_command == "tail":
            print(tail_text(config.log_path, lines=max(1, args.lines)), end="")
            return 0
        if args.diagnostics_command == "export":
            try:
                path = export_diagnostics(config, Path(args.path))
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            log.info("Sanitized diagnostics exported to %s", path)
            print(f"Wrote {path}")
            print("No catalogue database or photo files are included.")
            return 0
        if args.diagnostics_command == "runs":
            from ppa.activity_runs import concise_runs_text, load_activity_runs
            print(concise_runs_text(load_activity_runs(config.log_path, limit=max(1, args.limit))))
            return 0
        if args.diagnostics_command == "run-export":
            from ppa.activity_runs import export_run_transcript
            try:
                path = export_run_transcript(config, args.run_id, Path(args.path))
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr); return 1
            print(f"Wrote {path}")
            print("No catalogue database or photo files are included.")
            return 0
        return 1

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

    if args.command == "timeline":
        from ppa.timeline import build_timeline, concise_text as timeline_text
        try:
            view = build_timeline(conn, library_id=args.library_id,
                                  directory_prefix=args.directory)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(timeline_text(view))
        if args.json_path:
            Path(args.json_path).write_text(view.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "timeline-clusters":
        from ppa.timeline import build_timeline
        from ppa.timeline_clusters import build_clusters, concise_text as cluster_text
        try:
            view = build_timeline(conn, library_id=args.library_id,
                                  directory_prefix=args.directory)
            result = build_clusters(view)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(cluster_text(result))
        if args.json_path:
            Path(args.json_path).write_text(result.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "event-story":
        from ppa.events import get_event
        from ppa.event_story import build_event_story, concise_text as story_text
        from ppa.timeline import build_timeline
        try:
            event = get_event(conn, args.event_id)
            view = build_timeline(conn, library_id=event.library_id)
            story = build_event_story(conn, view, event.id)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(story_text(story))
        if args.json_path:
            Path(args.json_path).write_text(story.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "event-browse":
        from ppa.event_navigation import build_event_browse_index, concise_text as browse_text
        try:
            index = build_event_browse_index(conn, library_id=args.library_id)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(browse_text(index))
        if args.json_path:
            Path(args.json_path).write_text(index.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "event-home":
        from ppa.event_home import build_event_home, concise_text as home_text
        from ppa.timeline import build_timeline
        try:
            view = build_timeline(conn, library_id=args.library_id)
            home = build_event_home(conn, view)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(home_text(home))
        if args.json_path:
            Path(args.json_path).write_text(home.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "album-home":
        from ppa.album_home import build_album_home, concise_text as album_home_text
        try:
            home = build_album_home(conn, library_id=args.library_id)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(album_home_text(home))
        if args.json_path:
            Path(args.json_path).write_text(home.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0


    if args.command == "tag-home":
        from ppa.tag_home import build_tag_home, concise_text as tag_home_text
        try:
            home = build_tag_home(conn, library_id=args.library_id)
        except ValueError as exc:
            print(str(exc), file=sys.stderr); return 1
        print(tag_home_text(home))
        if args.json_path:
            Path(args.json_path).write_text(home.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "tag-intersection":
        from ppa.tag_home import build_tag_intersection_view
        try:
            view = build_tag_intersection_view(conn, library_id=args.library_id, tag_ids=tuple(args.tag_ids))
        except ValueError as exc:
            print(str(exc), file=sys.stderr); return 1
        print(f"{view.name}: {view.total_members} logical photos")
        if args.json_path:
            Path(args.json_path).write_text(view.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "event-health":
        from ppa.event_health import build_event_health_view, concise_text as health_text
        from ppa.timeline import build_timeline
        try:
            view = build_timeline(conn, library_id=args.library_id)
            health = build_event_health_view(conn, view)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(health_text(health))
        if args.json_path:
            Path(args.json_path).write_text(health.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "event-search":
        from ppa.event_home import build_event_home
        from ppa.event_search import build_event_search_index, search_event_index, concise_text as search_text
        from ppa.timeline import build_timeline
        try:
            view = build_timeline(conn, library_id=args.library_id)
            home = build_event_home(conn, view)
            index = build_event_search_index(conn, home)
            results = search_event_index(index, text=args.query, year=args.year,
                                         start_date=args.start_date, end_date=args.end_date,
                                         occasion=args.occasion, place=args.place, person=args.person)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(search_text(results))
        if args.json_path:
            Path(args.json_path).write_text(results.to_json() + "\n", encoding="utf-8")
            print(f"\nWrote {args.json_path}")
        return 0

    if args.command == "event-activity":
        from ppa.event_activity import list_favorite_event_ids, list_recent_event_ids, set_event_favorite
        from ppa.events import get_event
        try:
            if args.event_activity_command == "favorite":
                state = set_event_favorite(conn, args.event_id, not args.off)
                print(("Favourite" if state.favorite else "Not favourite") + f": {args.event_id}")
                return 0
            ids = (list_favorite_event_ids(conn, library_id=args.library_id)
                   if args.event_activity_command == "favorites"
                   else list_recent_event_ids(conn, library_id=args.library_id, limit=args.limit))
            for eid in ids:
                event = get_event(conn, eid)
                print(f"{eid}  {event.start_date}  {event.name}")
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr); return 1

    if args.command == "organize":
        from ppa.organization import (add_photo_to_album, create_album, create_tag, list_albums, list_tags,
                                      remove_photo_from_album, tag_photo, untag_photo)
        try:
            if args.organize_command == "albums":
                for album in list_albums(conn, library_id=args.library_id):
                    print(f"{album.id}  {album.name}  [{len(album.photo_ids)} photos]")
                return 0
            if args.organize_command == "album-create":
                album = create_album(conn, library_id=args.library_id, name=args.name, description=args.description)
                print(f"Created Album {album.name}: {album.id}"); return 0
            if args.organize_command == "album-add":
                album = add_photo_to_album(conn, args.album_id, args.photo_id)
                print(f"Album {album.name}: {len(album.photo_ids)} photos"); return 0
            if args.organize_command == "album-remove":
                album = remove_photo_from_album(conn, args.album_id, args.photo_id)
                print(f"Album {album.name}: {len(album.photo_ids)} photos"); return 0
            if args.organize_command == "tags":
                for tag in list_tags(conn, library_id=args.library_id):
                    print(f"{tag.id}  {tag.name}  [{len(tag.photo_ids)} photos]")
                return 0
            if args.organize_command == "tag-create":
                tag = create_tag(conn, library_id=args.library_id, name=args.name)
                print(f"Tag {tag.name}: {tag.id}"); return 0
            if args.organize_command == "tag-add":
                tag = tag_photo(conn, args.tag_id, args.photo_id)
                print(f"Tag {tag.name}: {len(tag.photo_ids)} photos"); return 0
            if args.organize_command == "tag-remove":
                tag = untag_photo(conn, args.tag_id, args.photo_id)
                print(f"Tag {tag.name}: {len(tag.photo_ids)} photos"); return 0
        except (ValueError, __import__('sqlite3').IntegrityError) as exc:
            print(str(exc), file=sys.stderr); return 1

    if args.command == "event-views":
        from ppa.event_views import delete_event_view, evaluate_saved_view, get_event_view, list_event_views, save_event_view
        if args.event_views_command == "list":
            try: views = list_event_views(conn, library_id=args.library_id)
            except ValueError as exc:
                print(str(exc), file=sys.stderr); return 1
            for v in views:
                filters = [f"query={v.query_text!r}"] if v.query_text else []
                for key, value in (("year", v.year), ("from", v.start_date), ("to", v.end_date), ("occasion", v.occasion_filter), ("place", v.place_filter), ("person", v.person_filter)):
                    if value is not None: filters.append(f"{key}={value}")
                print(f"{v.id}  {v.name}" + ("  [" + ", ".join(filters) + "]" if filters else ""))
            return 0
        if args.event_views_command == "save":
            try:
                v = save_event_view(conn, library_id=args.library_id, name=args.name, query_text=args.query,
                                    year=args.year, start_date=args.start_date, end_date=args.end_date,
                                    occasion_filter=args.occasion, place_filter=args.place, person_filter=args.person)
            except ValueError as exc:
                print(str(exc), file=sys.stderr); return 1
            print(f"Saved {v.name}: {v.id}"); return 0
        if args.event_views_command == "delete":
            if not delete_event_view(conn, args.view_id):
                print(f"saved Event view not found: {args.view_id}", file=sys.stderr); return 1
            print("Deleted"); return 0
        if args.event_views_command == "run":
            from ppa.event_home import build_event_home
            from ppa.event_search import build_event_search_index, concise_text as search_text
            from ppa.timeline import build_timeline
            try:
                v = get_event_view(conn, args.view_id)
                home = build_event_home(conn, build_timeline(conn, library_id=v.library_id))
                results = evaluate_saved_view(build_event_search_index(conn, home), v)
            except ValueError as exc:
                print(str(exc), file=sys.stderr); return 1
            print(search_text(results))
            if args.json_path:
                Path(args.json_path).write_text(results.to_json() + "\n", encoding="utf-8"); print(f"\nWrote {args.json_path}")
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
        if args.pilot_command == "queue":
            from ppa.review_queue import build_review_queue, concise_text as queue_text
            try:
                queue = build_review_queue(conn, library_id=args.library_id,
                                           directory_prefix=args.directory)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(queue_text(queue, include_d=args.all))
            if args.json_path:
                Path(args.json_path).write_text(queue.to_json() + "\n", encoding="utf-8")
                print(f"\nWrote {args.json_path}")
            return 0
        if args.pilot_command == "questions":
            from ppa.anchor_opportunities import build_anchor_questions, concise_text as question_text
            try:
                questions = build_anchor_questions(conn, library_id=args.library_id,
                                                    directory_prefix=args.directory)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(question_text(questions))
            if args.json_path:
                Path(args.json_path).write_text(questions.to_json() + "\n", encoding="utf-8")
                print(f"\nWrote {args.json_path}")
            return 0
        if args.pilot_command == "explain":
            from ppa.evidence_inspector import inspect_date_evidence, concise_text as evidence_text
            try:
                trace = inspect_date_evidence(conn, args.file_id)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(evidence_text(trace))
            if args.json_path:
                Path(args.json_path).write_text(trace.to_json() + "\n", encoding="utf-8")
                print(f"\nWrote {args.json_path}")
            return 0
        if args.pilot_command == "unresolved":
            from ppa.unresolved import build_unresolved_memories, concise_text as unresolved_text
            try:
                view = build_unresolved_memories(conn, library_id=args.library_id,
                                                 directory_prefix=args.directory)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(unresolved_text(view))
            if args.json_path:
                Path(args.json_path).write_text(view.to_json() + "\n", encoding="utf-8")
                print(f"\nWrote {args.json_path}")
            return 0
        if args.pilot_command == "audit":
            from ppa.pilot_audit import build_pilot_audit, concise_text as audit_text
            try:
                snap = build_pilot_audit(conn, library_id=args.library_id,
                                         directory_prefix=args.directory)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(audit_text(snap))
            if args.json_path:
                Path(args.json_path).write_text(snap.to_json() + "\n", encoding="utf-8")
                print(f"\nWrote {args.json_path}")
            return 0
        if args.pilot_command == "audit-compare":
            import json
            from ppa.pilot_audit import (snapshot_from_dict, compare_pilot_audits,
                                         comparison_text)
            try:
                before = snapshot_from_dict(json.loads(Path(args.before).read_text(encoding="utf-8")))
                after = snapshot_from_dict(json.loads(Path(args.after).read_text(encoding="utf-8")))
                comparison = compare_pilot_audits(before, after)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(comparison_text(comparison))
            if args.json_path:
                Path(args.json_path).write_text(comparison.to_json() + "\n", encoding="utf-8")
                print(f"\nWrote {args.json_path}")
            return 0
        if args.pilot_command == "session-start":
            from ppa.pilot_session import start_pilot_session, save_pilot_session, concise_text
            path = Path(args.session_path)
            if path.exists():
                print(f"pilot session already exists: {path}", file=sys.stderr)
                return 1
            try:
                session = start_pilot_session(conn, library_id=args.library_id,
                                              directory_prefix=args.directory)
                save_pilot_session(session, path)
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(concise_text(session))
            print(f"\nSaved {path}")
            return 0
        if args.pilot_command == "session-checkpoint":
            from ppa.pilot_session import (load_pilot_session, checkpoint_pilot_session,
                                           save_pilot_session, concise_text)
            path = Path(args.session_path)
            try:
                session = load_pilot_session(path)
                session = checkpoint_pilot_session(conn, session, label=args.label)
                save_pilot_session(session, path)
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(concise_text(session))
            print(f"\nUpdated {path}")
            return 0
        if args.pilot_command == "session-status":
            from ppa.pilot_session import load_pilot_session, concise_text
            try:
                session = load_pilot_session(Path(args.session_path))
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(concise_text(session))
            return 0
        if args.pilot_command == "session-close":
            from ppa.pilot_session import (load_pilot_session, close_pilot_session,
                                           save_pilot_session, concise_text)
            path = Path(args.session_path)
            try:
                session = load_pilot_session(path)
                session = close_pilot_session(conn, session)
                save_pilot_session(session, path)
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(concise_text(session))
            print(f"\nClosed {path}")
            return 0
        if args.pilot_command == "session-report":
            from ppa.pilot_audit import build_pilot_audit
            from ppa.pilot_session import load_pilot_session
            from ppa.review_report import export_review_progress
            try:
                session = load_pilot_session(Path(args.session_path))
                if session.status == "closed":
                    current = session.final
                else:
                    current = build_pilot_audit(
                        conn, library_id=session.library_id,
                        directory_prefix=session.directory_prefix,
                        file_ids=session.explicit_file_ids)
                    if (current.library_root, current.directory_prefix, current.explicit_file_ids) != (
                            session.library_root, session.directory_prefix, session.explicit_file_ids):
                        raise ValueError("pilot scope no longer resolves to the original library/root")
                path = export_review_progress(config, session, current, Path(args.output))
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"Shareable review progress report: {path}")
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
