"""Phase 9.9 — conservative, review-only organisation suggestions.

Suggestions are derived solely from explicit human curation already present in
Events, Albums, and Tags.  The engine currently proposes only *missing Tag*
review candidates inside a strongly tagged peer group.  It never invents Tag
names, never infers Album/Event membership, and never reads chronology or
metadata evidence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from sqlite3 import Connection

from ppa.organization import bulk_tag_photos
from ppa.organization_browse import OrganizationBrowseView, build_membership_browse

ORGANIZATION_SUGGESTIONS_SCHEMA = "ppa-organization-suggestions/1"
MIN_GROUP_PHOTOS = 5
MIN_TAGGED_PHOTOS = 4
MIN_COVERAGE = 0.80


@dataclass(frozen=True)
class OrganizationSuggestion:
    id: str
    library_id: int
    kind: str
    group_kind: str
    group_id: str
    group_name: str
    tag_id: str
    tag_name: str
    peer_count: int
    tagged_count: int
    coverage: float
    target_photo_ids: tuple[str, ...]
    rationale: str

    @property
    def target_count(self) -> int:
        return len(self.target_photo_ids)


@dataclass(frozen=True)
class OrganizationSuggestionsView:
    schema: str
    read_only: bool
    library_id: int
    suggestions: tuple[OrganizationSuggestion, ...]

    @property
    def candidate_photo_count(self) -> int:
        return len({pid for s in self.suggestions for pid in s.target_photo_ids})

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "read_only": self.read_only,
            "library_id": self.library_id,
            "candidate_photo_count": self.candidate_photo_count,
            "suggestions": [dict(asdict(s), target_count=s.target_count) for s in self.suggestions],
        }

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":"))


def _suggestion_id(*, kind: str, group_id: str, tag_id: str,
                   peers: tuple[str, ...], tagged: tuple[str, ...],
                   targets: tuple[str, ...]) -> str:
    payload = json.dumps({"kind": kind, "group_id": group_id, "tag_id": tag_id,
                          "peers": list(peers), "tagged": list(tagged),
                          "targets": list(targets)}, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _emit_group_suggestions(*, library_id: int, group_kind: str, group_id: str,
                            group_name: str, member_ids: set[str],
                            tags: tuple[tuple[str, str], ...],
                            tag_members: dict[str, set[str]]) -> list[OrganizationSuggestion]:
    if len(member_ids) < MIN_GROUP_PHOTOS:
        return []
    out: list[OrganizationSuggestion] = []
    for tag_id, tag_name in tags:
        tagged = member_ids & tag_members.get(tag_id, set())
        count = len(tagged)
        if count < MIN_TAGGED_PHOTOS:
            continue
        coverage = count / len(member_ids)
        if coverage < MIN_COVERAGE or count == len(member_ids):
            continue
        targets = tuple(sorted(member_ids - tagged))
        if not targets:
            continue
        kind = f"{group_kind}_tag_gap"
        pct = round(coverage * 100)
        rationale = (
            f"{count} of {len(member_ids)} logical photos in {group_kind} "
            f"'{group_name}' already have Tag '{tag_name}' ({pct}%). "
            f"Review the remaining {len(targets)} before applying it."
        )
        out.append(OrganizationSuggestion(
            _suggestion_id(kind=kind, group_id=group_id, tag_id=tag_id,
                           peers=tuple(sorted(member_ids)), tagged=tuple(sorted(tagged)),
                           targets=targets),
            library_id, kind, group_kind, group_id, group_name, tag_id, tag_name,
            len(member_ids), count, coverage, targets, rationale,
        ))
    return out


def build_organization_suggestions(conn: Connection, *, library_id: int,
                                   include_reviewed: bool = False) -> OrganizationSuggestionsView:
    """Build a deterministic read-only suggestion projection.

    The projection deliberately uses only organisation/event membership.  It
    does not read metadata_observations, anchors, reconstructions, or Timeline
    state.
    """
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    before = conn.total_changes

    tags = tuple((r["id"], r["name"]) for r in conn.execute(
        "SELECT id,name FROM tags WHERE library_id=? ORDER BY name COLLATE NOCASE,id", (library_id,)
    ))
    tag_members: dict[str, set[str]] = {tid: set() for tid, _ in tags}
    for r in conn.execute(
        "SELECT pt.tag_id,pt.photo_id FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id "
        "WHERE t.library_id=? ORDER BY pt.tag_id,pt.photo_id", (library_id,)
    ):
        tag_members.setdefault(r["tag_id"], set()).add(r["photo_id"])

    suggestions: list[OrganizationSuggestion] = []

    albums = tuple(conn.execute(
        "SELECT id,name FROM albums WHERE library_id=? ORDER BY name COLLATE NOCASE,id", (library_id,)
    ))
    album_members: dict[str, set[str]] = {r["id"]: set() for r in albums}
    for r in conn.execute(
        "SELECT ap.album_id,ap.photo_id FROM album_photos ap JOIN albums a ON a.id=ap.album_id "
        "WHERE a.library_id=? ORDER BY ap.album_id,ap.photo_id", (library_id,)
    ):
        album_members.setdefault(r["album_id"], set()).add(r["photo_id"])
    for album in albums:
        suggestions.extend(_emit_group_suggestions(
            library_id=library_id, group_kind="album", group_id=album["id"],
            group_name=album["name"], member_ids=album_members.get(album["id"], set()),
            tags=tags, tag_members=tag_members,
        ))

    events = tuple(conn.execute(
        "SELECT id,name FROM events WHERE library_id=? ORDER BY start_date,end_date,name COLLATE NOCASE,id",
        (library_id,),
    ))
    event_members: dict[str, set[str]] = {r["id"]: set() for r in events}
    # Events attach to Files; suggestions attach to logical Photos.  DISTINCT
    # logical identity avoids duplicate-copy inflation of support ratios.
    for r in conn.execute(
        "SELECT DISTINCT em.event_id,f.photo_id FROM event_members em "
        "JOIN events e ON e.id=em.event_id JOIN files f ON f.id=em.file_id "
        "WHERE e.library_id=? AND f.library_id=? ORDER BY em.event_id,f.photo_id",
        (library_id, library_id),
    ):
        event_members.setdefault(r["event_id"], set()).add(r["photo_id"])
    for event in events:
        suggestions.extend(_emit_group_suggestions(
            library_id=library_id, group_kind="event", group_id=event["id"],
            group_name=event["name"], member_ids=event_members.get(event["id"], set()),
            tags=tags, tag_members=tag_members,
        ))

    suggestions.sort(key=lambda s: (
        -s.coverage, -s.tagged_count, s.group_kind, s.group_name.casefold(),
        s.tag_name.casefold(), s.id,
    ))
    if not include_reviewed:
        suppressed = {r["suggestion_id"] for r in conn.execute(
            "SELECT suggestion_id FROM organization_suggestion_reviews "
            "WHERE library_id=? AND status='dismissed'", (library_id,)
        )}
        suggestions = [s for s in suggestions if s.id not in suppressed]
    if conn.total_changes != before:
        raise RuntimeError("organisation suggestions projection must be read-only")
    return OrganizationSuggestionsView(
        ORGANIZATION_SUGGESTIONS_SCHEMA, True, library_id, tuple(suggestions)
    )


def build_suggestion_browse(conn: Connection, suggestion: OrganizationSuggestion) -> OrganizationBrowseView:
    return build_membership_browse(
        conn, library_id=suggestion.library_id, photo_ids=suggestion.target_photo_ids,
        object_kind="organization_suggestion", object_id=suggestion.id,
        name=f"Review: {suggestion.tag_name} in {suggestion.group_name}",
        description=suggestion.rationale,
    )


def _write_review_rows(conn: Connection, suggestion: OrganizationSuggestion, *,
                       status: str, action: str, note: str | None, now: str) -> None:
    conn.execute(
        "INSERT INTO organization_suggestion_reviews(library_id,suggestion_id,status,note,reviewed_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(library_id,suggestion_id) DO UPDATE SET "
        "status=excluded.status,note=excluded.note,reviewed_at=excluded.reviewed_at",
        (suggestion.library_id, suggestion.id, status, note, now),
    )
    conn.execute(
        "INSERT INTO organization_suggestion_review_history(library_id,suggestion_id,action,note,created_at) "
        "VALUES (?,?,?,?,?)",
        (suggestion.library_id, suggestion.id, action, note, now),
    )


def apply_organization_suggestion(conn: Connection, suggestion: OrganizationSuggestion, *,
                                  note: str | None = None):
    """Apply one approved suggestion and record acceptance atomically."""
    current = build_organization_suggestions(conn, library_id=suggestion.library_id, include_reviewed=True)
    fresh = next((s for s in current.suggestions if s.id == suggestion.id), None)
    if fresh is None or fresh != suggestion:
        raise ValueError("organisation suggestion is stale; refresh and review again")
    note = _clean_note(note); now = _now()
    try:
        conn.execute("BEGIN")
        result = bulk_tag_photos(conn, suggestion.tag_id, suggestion.target_photo_ids, _manage_transaction=False)
        _write_review_rows(conn, suggestion, status="accepted", action="accept", note=note, now=now)
        conn.commit()
        return result
    except Exception:
        conn.rollback(); raise


@dataclass(frozen=True)
class SuggestionReview:
    library_id: int
    suggestion_id: str
    status: str
    note: str | None
    reviewed_at: str


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    text = str(note).strip()
    if not text:
        return None
    if len(text) > 2000:
        raise ValueError("suggestion review note is too long")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_review(conn: Connection, suggestion: OrganizationSuggestion, *, status: str,
                   action: str, note: str | None) -> SuggestionReview:
    note = _clean_note(note); now = _now()
    try:
        conn.execute("BEGIN")
        _write_review_rows(conn, suggestion, status=status, action=action, note=note, now=now)
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return SuggestionReview(suggestion.library_id, suggestion.id, status, note, now)


def dismiss_organization_suggestion(conn: Connection, suggestion: OrganizationSuggestion, *,
                                    note: str | None = None) -> SuggestionReview:
    current = build_organization_suggestions(conn, library_id=suggestion.library_id, include_reviewed=True)
    fresh = next((s for s in current.suggestions if s.id == suggestion.id), None)
    if fresh is None or fresh != suggestion:
        raise ValueError("organisation suggestion is stale; refresh and review again")
    return _record_review(conn, suggestion, status="dismissed", action="dismiss", note=note)


def restore_organization_suggestion(conn: Connection, *, library_id: int, suggestion_id: str,
                                    note: str | None = None) -> bool:
    note = _clean_note(note)
    row = conn.execute(
        "SELECT 1 FROM organization_suggestion_reviews WHERE library_id=? AND suggestion_id=? AND status='dismissed'",
        (library_id, suggestion_id),
    ).fetchone()
    if row is None:
        return False
    now = _now()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM organization_suggestion_reviews WHERE library_id=? AND suggestion_id=?",
                     (library_id, suggestion_id))
        conn.execute(
            "INSERT INTO organization_suggestion_review_history(library_id,suggestion_id,action,note,created_at) "
            "VALUES (?,?,'restore',?,?)", (library_id, suggestion_id, note, now))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return True


def list_suggestion_reviews(conn: Connection, *, library_id: int,
                            status: str | None = None) -> tuple[SuggestionReview, ...]:
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")
    if status not in {None, "dismissed", "accepted"}:
        raise ValueError("unknown suggestion review status")
    sql = "SELECT * FROM organization_suggestion_reviews WHERE library_id=?"
    args: list[object] = [library_id]
    if status is not None:
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY reviewed_at DESC,suggestion_id"
    return tuple(SuggestionReview(r["library_id"], r["suggestion_id"], r["status"], r["note"], r["reviewed_at"])
                 for r in conn.execute(sql, tuple(args)))


def concise_text(view: OrganizationSuggestionsView) -> str:
    lines = [
        "PPA Assisted Organisation Suggestions",
        "====================================",
        f"Library: {view.library_id}",
        f"Suggestions: {len(view.suggestions)}",
        f"Unique candidate photos: {view.candidate_photo_count}",
    ]
    for s in view.suggestions:
        lines.append(
            f"- [{s.group_kind}] {s.group_name}: Tag '{s.tag_name}' — "
            f"{s.tagged_count}/{s.peer_count} peers; review {s.target_count}"
        )
    return "\n".join(lines)
