"""Integrity verification (Phase 2).

Scanning trusts a file's stored hash when its size and mtime are unchanged,
so routine re-scans stay fast. That trust is the right default, but it means
a scan will not notice *silent* corruption: bit rot, a bad sector, or an
external tool that rewrote the bytes while preserving the timestamp.

verify_library is the deliberate, explicit counter to that. It re-reads and
re-hashes catalogued files and compares against the recorded hash. It never
"repairs" anything and never rewrites the stored hash on a mismatch — a
mismatch is a warning for the user to investigate against their backups, not
a new truth to silently adopt. Everything it finds is appended to
integrity_events; source files are only ever read.

Run it on a schedule or before a backup, not on every scan.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from PIL import Image, UnidentifiedImageError

from ppa.hashing import sha256_file
from ppa.logging_setup import get_logger

log = get_logger("integrity")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class VerifyReport:
    started_at: str
    completed_at: str | None = None
    verified_ok: int = 0
    mismatches: int = 0
    backfilled: int = 0
    now_missing: int = 0
    corrupt: int = 0
    unavailable_libraries: int = 0
    skipped_unavailable: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    def summary(self) -> str:
        lines = [
            "Integrity verification",
            f"  verified ok:      {self.verified_ok}",
            f"  hash mismatches:  {self.mismatches}",
            f"  hashes backfilled:{self.backfilled}",
            f"  now missing:      {self.now_missing}",
            f"  unreadable:       {self.corrupt}",
            f"  offline libraries:{self.unavailable_libraries}",
            f"  skipped (offline):{self.skipped_unavailable}",
        ]
        return "\n".join(lines)


def verify_library(
    conn: Connection,
    progress_cb: Callable[[str], None] | None = None,
) -> VerifyReport:
    """Re-hash every active file and reconcile against its recorded hash.

    Read-only with respect to source files. Records findings in
    integrity_events and returns a summary. Does not alter a stored hash when
    it finds a mismatch.

    ``progress_cb`` is an optional callable given short status strings as the
    (potentially slow) re-hashing proceeds; it has no effect on behaviour.
    """
    def _progress(msg: str) -> None:
        if progress_cb is not None:
            progress_cb(msg)

    report = VerifyReport(started_at=_now())

    # Determine which libraries are currently reachable. If a library root is
    # offline (external drive unplugged, network share down, drive-letter
    # changed), we must NOT conclude its photos are missing — we only know the
    # library is unavailable. Record that and skip its files entirely.
    lib_available: dict[int, bool] = {}
    for lib in conn.execute("SELECT id, root_canonical_path FROM libraries").fetchall():
        available = os.path.isdir(lib["root_canonical_path"])
        lib_available[lib["id"]] = available
        conn.execute(
            "UPDATE libraries SET state = ? WHERE id = ?",
            ("active" if available else "unavailable", lib["id"]),
        )
        if not available:
            report.unavailable_libraries += 1

    rows = conn.execute(
        """
        SELECT f.id, f.path, f.health_status, f.current_revision_id, f.library_id,
               r.sha256 AS revision_sha
        FROM files f
        LEFT JOIN file_revisions r ON r.id = f.current_revision_id
        WHERE f.presence_status = 'present'
        """
    ).fetchall()
    total = len(rows)

    for i, row in enumerate(rows, start=1):
        if i % 25 == 0 or i == total:
            _progress(f"Verifying… {i}/{total}")

        # Skip files whose library is unreachable: absence can't be concluded.
        if row["library_id"] is not None and not lib_available.get(row["library_id"], True):
            report.skipped_unavailable += 1
            continue

        path = Path(row["path"])

        if not path.exists():
            conn.execute(
                "UPDATE files SET status = 'missing', presence_status = 'missing' "
                "WHERE id = ?",
                (row["id"],),
            )
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail) "
                "VALUES (?, 'missing', ?)",
                (row["id"], f"Not present at {path} during integrity verification."),
            )
            report.now_missing += 1
            report.problems.append((str(path), "missing"))
            continue

        # A file we cannot decode/read is a health problem, not absence.
        try:
            with Image.open(path) as img:
                img.verify()
        except (UnidentifiedImageError, OSError) as exc:
            conn.execute(
                "UPDATE files SET health_status = 'unreadable' WHERE id = ?", (row["id"],)
            )
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail) "
                "VALUES (?, 'unreadable', ?)",
                (row["id"], f"Failed to decode during verification: {exc}"),
            )
            report.corrupt += 1
            report.problems.append((str(path), f"unreadable: {exc}"))
            continue

        try:
            actual = sha256_file(path)
        except OSError as exc:
            conn.execute(
                "UPDATE files SET health_status = 'unreadable' WHERE id = ?", (row["id"],)
            )
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail) "
                "VALUES (?, 'unreadable', ?)",
                (row["id"], f"Failed to hash during verification: {exc}"),
            )
            report.corrupt += 1
            report.problems.append((str(path), f"unhashable: {exc}"))
            continue

        stored = row["revision_sha"]
        if stored is None:
            # Complete the current revision's content identity (and the file
            # mirror) — the revision is authoritative, so backfill it too.
            if row["current_revision_id"]:
                conn.execute(
                    "UPDATE file_revisions SET sha256 = ? WHERE id = ?",
                    (actual, row["current_revision_id"]),
                )
            conn.execute(
                "UPDATE files SET sha256 = ?, hash_computed_at = ?, health_status = 'ok' "
                "WHERE id = ?",
                (actual, _now(), row["id"]),
            )
            report.backfilled += 1
        elif stored == actual:
            report.verified_ok += 1
            if row["health_status"] != "ok":
                # Content matches again (e.g. an original was restored) -> healthy.
                conn.execute(
                    "UPDATE files SET health_status = 'ok' WHERE id = ?", (row["id"],)
                )
        else:
            # Positive evidence the bytes disagree with the recorded identity.
            # Flag health; NEVER overwrite the trusted revision hash here.
            conn.execute(
                "UPDATE files SET health_status = 'hash_mismatch' WHERE id = ?",
                (row["id"],),
            )
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail) "
                "VALUES (?, 'hash_mismatch', ?)",
                (
                    row["id"],
                    f"Recorded content {stored} but file now hashes {actual}. "
                    "Content changed with no scan detecting it (possible corruption "
                    "or external edit). Recorded revision left intact for investigation.",
                ),
            )
            # Phase 12.3 keeps the machine-readable observation separately from
            # the human prose event.  Forensic tools must never parse an event
            # message to recover the bytes Verify actually observed.
            try:
                st = path.stat()
                observed_size = int(st.st_size)
                observed_mtime_ns = int(st.st_mtime_ns)
            except OSError:
                observed_size = None
                observed_mtime_ns = None
            conn.execute(
                "INSERT INTO integrity_mismatch_observations "
                "(file_id, expected_revision_id, expected_sha256, observed_sha256, "
                " observed_path, observed_size_bytes, observed_mtime_ns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["current_revision_id"], stored, actual,
                    str(path), observed_size, observed_mtime_ns,
                ),
            )
            report.mismatches += 1
            report.problems.append((str(path), "hash mismatch"))

    conn.commit()
    report.completed_at = _now()
    log.info("Verification complete\n%s", report.summary())
    return report
