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
    problems: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    def summary(self) -> str:
        lines = [
            "Integrity verification",
            f"  verified ok:      {self.verified_ok}",
            f"  hash mismatches:  {self.mismatches}",
            f"  hashes backfilled:{self.backfilled}",
            f"  now missing:      {self.now_missing}",
            f"  unreadable:       {self.corrupt}",
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

    rows = conn.execute(
        "SELECT id, path, sha256 FROM files WHERE status = 'active'"
    ).fetchall()
    total = len(rows)

    for i, row in enumerate(rows, start=1):
        if i % 25 == 0 or i == total:
            _progress(f"Verifying… {i}/{total}")
        path = Path(row["path"])

        if not path.exists():
            conn.execute("UPDATE files SET status = 'missing' WHERE id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail) "
                "VALUES (?, 'missing', ?)",
                (row["id"], f"Not present at {path} during integrity verification."),
            )
            report.now_missing += 1
            report.problems.append((str(path), "missing"))
            continue

        # A file we cannot decode/read is a corruption signal in its own right.
        try:
            with Image.open(path) as img:
                img.verify()
        except (UnidentifiedImageError, OSError) as exc:
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail) "
                "VALUES (?, 'corrupt', ?)",
                (row["id"], f"Failed to decode during verification: {exc}"),
            )
            report.corrupt += 1
            report.problems.append((str(path), f"unreadable: {exc}"))
            continue

        try:
            actual = sha256_file(path)
        except OSError as exc:
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail) "
                "VALUES (?, 'corrupt', ?)",
                (row["id"], f"Failed to hash during verification: {exc}"),
            )
            report.corrupt += 1
            report.problems.append((str(path), f"unhashable: {exc}"))
            continue

        stored = row["sha256"]
        if stored is None:
            conn.execute(
                "UPDATE files SET sha256 = ?, hash_computed_at = ? WHERE id = ?",
                (actual, _now(), row["id"]),
            )
            report.backfilled += 1
        elif stored == actual:
            report.verified_ok += 1
        else:
            # Do NOT overwrite the stored hash. Record and let the user decide.
            conn.execute(
                "INSERT INTO integrity_events (file_id, event_type, detail) "
                "VALUES (?, 'hash_mismatch', ?)",
                (
                    row["id"],
                    f"Stored {stored[:12]}... but file now hashes {actual[:12]}.... "
                    "Content changed with no scan detecting it (possible corruption "
                    "or external edit). Stored hash left unchanged for investigation.",
                ),
            )
            report.mismatches += 1
            report.problems.append((str(path), "hash mismatch"))

    conn.commit()
    report.completed_at = _now()
    log.info("Verification complete\n%s", report.summary())
    return report
