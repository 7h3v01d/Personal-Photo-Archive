"""Phase 9.12 — sanitized, shareable organisation curation report.

The report is a read-only projection of human curation state. It deliberately
excludes archive identity (paths, UUIDs/file ids, hashes, database details,
thumbnails and source bytes) and never mutates the archive.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from ppa.album_home import build_album_home
from ppa.organization_activity import build_organization_activity
from ppa.organization_health import build_organization_health
from ppa.organization_suggestions import list_suggestion_reviews
from ppa.organization_views import list_organization_views
from ppa.tag_home import build_tag_home
from ppa.diagnostics import sanitize_text

ORGANIZATION_REPORT_SCHEMA = "ppa-organization-report/1"


@dataclass(frozen=True)
class OrganizationReport:
    schema: str
    generated_at: str
    read_only: bool
    summary: dict
    albums: tuple[dict, ...]
    tags: tuple[dict, ...]
    saved_views: tuple[dict, ...]
    suggestion_reviews: tuple[dict, ...]
    recent_activity: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "read_only": self.read_only,
            "summary": self.summary,
            "albums": list(self.albums),
            "tags": list(self.tags),
            "saved_views": list(self.saved_views),
            "suggestion_reviews": list(self.suggestion_reviews),
            "recent_activity": list(self.recent_activity),
        }

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


def _require_library(conn: Connection, library_id: int) -> None:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")


def _privacy_pairs(conn: Connection, library_id: int) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    home = str(Path.home())
    if home:
        pairs.append((home, "<HOME>"))
    for r in conn.execute(
        "SELECT root_display_path,root_canonical_path FROM libraries WHERE id=?", (library_id,)):
        for value in (r["root_display_path"], r["root_canonical_path"]):
            if value:
                pairs.append((str(value), "<LIBRARY>"))
    for r in conn.execute("PRAGMA database_list"):
        value = r["file"]
        if value:
            pairs.append((str(Path(value).parent), "<PPA_DATA>"))
            pairs.append((str(value), "<PPA_DB>"))
    # Longest first prevents a parent replacement from exposing a deeper suffix.
    return tuple(sorted(set(pairs), key=lambda x: len(x[0]), reverse=True))


_UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b")
_HASH_RE = re.compile(r"(?i)\b[0-9a-f]{64}\b")
_WIN_ABS_RE = re.compile(r'(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/][^\r\n;<>|\"]+')
_UNC_RE = re.compile(r'\\\\[^\s\\/]+[\\/][^\r\n;<>|\"]+')
_POSIX_PRIVATE_RE = re.compile(r'(?<![A-Za-z0-9_])/(?:home|Users|mnt|media|Volumes)/[^\r\n;<>|\"]+')


def _sanitize_share_text(value: str | None, pairs) -> str | None:
    if value is None:
        return None
    out = sanitize_text(str(value), pairs)
    out = _WIN_ABS_RE.sub("<PRIVATE_PATH>", out)
    out = _UNC_RE.sub("<PRIVATE_PATH>", out)
    out = _POSIX_PRIVATE_RE.sub("<PRIVATE_PATH>", out)
    out = _UUID_RE.sub("<IDENTIFIER>", out)
    out = _HASH_RE.sub("<HASH>", out)
    return out


def _shareable_activity_change(entry, pairs) -> str:
    label = "Album" if entry.object_kind == "album" else "Tag"
    name = _sanitize_share_text(entry.object_name, pairs) or "[unnamed]"
    if entry.action == "create":
        return f"Created {label} '{name}'"
    if entry.action == "rename":
        old = _sanitize_share_text(entry.old_value, pairs) or ""
        new = _sanitize_share_text(entry.new_value, pairs) or name
        return f"Renamed {label} '{old}' → '{new}'"
    if entry.action == "description":
        return f"Updated description for Album '{name}'"
    if entry.action == "add_photo":
        return f"Added a photo to {label} '{name}'"
    if entry.action == "remove_photo":
        return f"Removed a photo from {label} '{name}'"
    if entry.action == "undo_add_photo":
        return f"Undid an add to {label} '{name}'"
    if entry.action == "undo_remove_photo":
        return f"Undid a removal from {label} '{name}'"
    return f"{label} '{name}': {entry.action.replace('_', ' ')}"


def build_organization_report(conn: Connection, *, library_id: int,
                              activity_limit: int = 100) -> OrganizationReport:
    """Build one sanitized report projection without exposing archive identifiers."""
    _require_library(conn, library_id)
    before = conn.total_changes
    album_home = build_album_home(conn, library_id=library_id)
    tag_home = build_tag_home(conn, library_id=library_id)
    health = build_organization_health(conn, library_id=library_id)
    saved = list_organization_views(conn, library_id=library_id)
    reviews = list_suggestion_reviews(conn, library_id=library_id)
    activity = build_organization_activity(conn, library_id=library_id, limit=activity_limit)

    pairs = _privacy_pairs(conn, library_id)
    album_names = {r["id"]: (_sanitize_share_text(r["name"], pairs) or "") for r in conn.execute(
        "SELECT id,name FROM albums WHERE library_id=?", (library_id,))}
    tag_names = {r["id"]: (_sanitize_share_text(r["name"], pairs) or "") for r in conn.execute(
        "SELECT id,name FROM tags WHERE library_id=?", (library_id,))}

    albums = tuple({
        "name": _sanitize_share_text(c.name, pairs),
        "description": _sanitize_share_text(c.description, pairs),
        "photo_count": c.photo_count,
        "present_count": c.present_count,
        "missing_only_count": c.missing_only_count,
        "custom_cover": c.has_custom_cover,
        "custom_order": c.has_custom_order,
    } for c in album_home.cards)
    tags = tuple({
        "name": _sanitize_share_text(c.name, pairs),
        "photo_count": c.photo_count,
        "present_count": c.present_count,
        "missing_only_count": c.missing_only_count,
    } for c in tag_home.cards)
    saved_views = tuple({
        "name": _sanitize_share_text(v.name, pairs),
        "albums": [album_names.get(x, "[missing Album]") for x in v.album_ids],
        "tags": [tag_names.get(x, "[missing Tag]") for x in v.tag_ids],
    } for v in saved)
    suggestion_reviews = tuple({
        "status": r.status,
        "note": _sanitize_share_text(r.note, pairs),
        "reviewed_at": r.reviewed_at,
    } for r in reviews)
    recent_activity = tuple({
        "when": e.created_at,
        "kind": e.object_kind,
        "object": _sanitize_share_text(e.object_name, pairs),
        "change": _shareable_activity_change(e, pairs),
        "undoable": e.undoable,
    } for e in activity.entries)
    summary = {
        "logical_photos": health.total_photos,
        "album_count": len(album_home.cards),
        "tag_count": len(tag_home.cards),
        "unorganized_count": health.unorganized_count,
        "no_album_count": health.no_album_count,
        "no_tag_count": health.no_tag_count,
        "empty_album_count": len(health.empty_album_ids),
        "unused_tag_count": len(health.unused_tag_ids),
        "albums_with_missing_only_members": len(health.albums_with_missing_only_members),
        "tags_with_missing_only_members": len(health.tags_with_missing_only_members),
        "broken_saved_view_count": len(health.broken_saved_view_ids),
        "saved_view_count": len(saved),
        "accepted_suggestion_count": sum(r.status == "accepted" for r in reviews),
        "dismissed_suggestion_count": sum(r.status == "dismissed" for r in reviews),
        "recent_activity_count": len(activity.entries),
    }
    if conn.total_changes != before:
        raise RuntimeError("organisation report projection must be read-only")
    return OrganizationReport(
        ORGANIZATION_REPORT_SCHEMA, datetime.now(timezone.utc).isoformat(), True,
        summary, albums, tags, saved_views, suggestion_reviews, recent_activity)


def markdown_text(report: OrganizationReport) -> str:
    s = report.summary
    lines = [
        "# Personal Photo Archive — Organisation Report", "",
        f"Generated: {report.generated_at}", "",
        "This report contains human curation summaries only. It excludes source-photo paths, archive identifiers, hashes, thumbnails and database internals.", "",
        "## Summary", "",
        f"- Logical photos: {s['logical_photos']}",
        f"- Albums: {s['album_count']}", f"- Tags: {s['tag_count']}",
        f"- Unorganised photos: {s['unorganized_count']}",
        f"- Photos with no Album: {s['no_album_count']}",
        f"- Photos with no Tags: {s['no_tag_count']}",
        f"- Empty Albums: {s['empty_album_count']}", f"- Unused Tags: {s['unused_tag_count']}",
        f"- Broken saved views: {s['broken_saved_view_count']}", "", "## Albums", "",
    ]
    if report.albums:
        for a in report.albums:
            desc = f" — {a['description']}" if a['description'] else ""
            lines.append(f"- **{a['name']}** — {a['photo_count']} photos ({a['missing_only_count']} missing-only){desc}")
    else: lines.append("- None")
    lines += ["", "## Tags", ""]
    if report.tags:
        for t in report.tags:
            lines.append(f"- **{t['name']}** — {t['photo_count']} photos ({t['missing_only_count']} missing-only)")
    else: lines.append("- None")
    lines += ["", "## Saved discovery views", ""]
    if report.saved_views:
        for v in report.saved_views:
            recipe = [*(f"Album: {x}" for x in v['albums']), *(f"Tag: {x}" for x in v['tags'])]
            lines.append(f"- **{v['name']}** — " + " ∩ ".join(recipe))
    else: lines.append("- None")
    lines += ["", "## Assisted-organisation reviews", ""]
    if report.suggestion_reviews:
        for r in report.suggestion_reviews:
            note = f" — {r['note']}" if r['note'] else ""
            lines.append(f"- {r['reviewed_at']} — **{r['status']}**{note}")
    else: lines.append("- None")
    lines += ["", "## Recent organisation activity", ""]
    if report.recent_activity:
        for e in report.recent_activity:
            lines.append(f"- {e['when']} — {e['change']}")
    else: lines.append("- None")
    return "\n".join(lines) + "\n"


def export_organization_report_zip(conn: Connection, *, library_id: int,
                                   output_path: str | Path,
                                   activity_limit: int = 100) -> Path:
    report = build_organization_report(conn, library_id=library_id, activity_limit=activity_limit)
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    readme = (
        "Personal Photo Archive — Shareable Organisation Report\n\n"
        "This ZIP intentionally excludes source photographs, thumbnails, database files, "
        "filesystem paths, archive identifiers and hashes.\n"
    )
    fd, tmp_name = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=str(out.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("organization-report.json", report.to_json() + "\n")
            zf.writestr("organization-report.md", markdown_text(report))
            zf.writestr("README.txt", readme)
        os.replace(tmp, out)
    finally:
        if tmp.exists(): tmp.unlink(missing_ok=True)
    return out
