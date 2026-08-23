"""Operational diagnostics and safe export helpers.

Diagnostics are deliberately outside the archive evidence model.  They may
observe application activity and catalogue health, but never modify photos,
metadata observations, anchors, reconstructions, or decisions.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ppa.db import connect, current_schema_version

DIAGNOSTICS_SCHEMA = "ppa-diagnostics/1"


@dataclass(frozen=True)
class DiagnosticsManifest:
    schema: str
    generated_at: str
    python: str
    platform: str
    schema_version: int | None
    log_level: str
    text_log: str
    structured_log: str
    redactions: tuple[str, ...]
    contents: tuple[str, ...]
    explicit_exclusions: tuple[str, ...]


def structured_log_path(log_path: Path) -> Path:
    return log_path.with_name(log_path.stem + ".jsonl")


def tail_text(path: Path, *, lines: int = 500) -> str:
    """Return the final *lines* of a UTF-8-ish text file without failing hard."""
    if lines <= 0 or not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = fh.readlines()
    except OSError:
        return ""
    return "".join(data[-lines:])


def _library_roots(db_path: Path) -> list[Path]:
    if not db_path.exists():
        return []
    conn = None
    try:
        conn = connect(db_path)
        rows = conn.execute("SELECT canonical_path FROM libraries ORDER BY id").fetchall()
        return [Path(r["canonical_path"]) for r in rows if r["canonical_path"]]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def _redaction_pairs(config) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    home = str(Path.home())
    if home:
        pairs.append((home, "<HOME>"))
    data_dir = str(config.db_path.parent)
    if data_dir and data_dir != home:
        pairs.append((data_dir, "<PPA_DATA>"))
    for idx, root in enumerate(_library_roots(config.db_path), 1):
        value = str(root)
        if value:
            pairs.append((value, f"<LIBRARY_{idx}>") )
    # Longest first so nested paths do not leak after a shorter replacement.
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def sanitize_text(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    out = text
    for raw, replacement in pairs:
        if raw:
            out = out.replace(raw, replacement)
            # Windows log strings can differ only by slash direction.
            out = out.replace(raw.replace("\\", "/"), replacement)
            out = out.replace(raw.replace("/", "\\"), replacement)
    # Defensive scrub for obvious Windows user-profile paths not caught above.
    out = re.sub(r"(?i)\b[A-Z]:\\Users\\[^\\\s|]+", r"<USER_HOME>", out)
    return out


def _candidate_log_files(log_path: Path) -> list[Path]:
    files: list[Path] = []
    for base in (log_path, structured_log_path(log_path)):
        if base.exists():
            files.append(base)
        for i in range(1, 6):
            rotated = Path(str(base) + f".{i}")
            if rotated.exists():
                files.append(rotated)
    return files


def export_diagnostics(config, destination: Path) -> Path:
    """Create a sanitized, shareable diagnostics ZIP.

    The catalogue DB, thumbnails, pilot session artifacts, and source photos are
    intentionally excluded. Absolute home/library paths are redacted from logs.
    """
    destination = Path(destination)
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)

    pairs = _redaction_pairs(config)
    members: list[tuple[str, str]] = []
    for path in _candidate_log_files(config.log_path):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        members.append((f"logs/{path.name}", sanitize_text(raw, pairs)))

    schema_version = None
    if config.db_path.exists():
        conn = None
        try:
            conn = connect(config.db_path)
            schema_version = current_schema_version(conn)
        except Exception:
            schema_version = None
        finally:
            if conn is not None:
                conn.close()

    manifest = DiagnosticsManifest(
        schema=DIAGNOSTICS_SCHEMA,
        generated_at=datetime.now(timezone.utc).isoformat(),
        python=platform.python_version(),
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        schema_version=schema_version,
        log_level=config.log_level,
        text_log=config.log_path.name,
        structured_log=structured_log_path(config.log_path).name,
        redactions=tuple(rep for _raw, rep in pairs),
        contents=tuple(name for name, _ in members),
        explicit_exclusions=(
            "catalogue database",
            "source photos",
            "thumbnails",
            "pilot session files",
            "raw configuration paths",
        ),
    )

    tmp = destination.with_name(destination.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(asdict(manifest), indent=2, sort_keys=True))
            readme = (
                "Personal Photo Archive diagnostics export\n"
                "==========================================\n\n"
                "This bundle contains sanitized operational logs only.\n"
                "It intentionally excludes the catalogue database, source photos, thumbnails,\n"
                "pilot session artifacts, and raw configuration paths.\n"
            )
            zf.writestr("README.txt", readme)
            for name, text in members:
                zf.writestr(name, text)
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return destination
