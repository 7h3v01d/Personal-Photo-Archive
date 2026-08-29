"""Operational run correlation over PPA's structured JSONL log.

Run records are diagnostics only.  They are not catalogue evidence and never
participate in chronology or archive authority.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

RUN_TRANSCRIPT_SCHEMA = "ppa-run-transcript/1"


@dataclass(frozen=True)
class RunEvent:
    timestamp: str
    run_id: str
    operation: str
    phase: str
    outcome: str | None
    message: str
    elapsed_ms: int | None = None
    detail: dict | None = None


@dataclass(frozen=True)
class ActivityRun:
    run_id: str
    operation: str
    started_at: str
    ended_at: str | None
    outcome: str
    elapsed_ms: int | None
    events: tuple[RunEvent, ...]


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def run_extra(run_id: str, operation: str, phase: str, *, outcome: str | None = None,
              elapsed_ms: int | None = None, detail: dict | None = None) -> dict:
    return {
        "run_id": run_id,
        "operation": operation,
        "run_phase": phase,
        "run_outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "run_detail": detail,
    }


def _iter_jsonl(paths: Iterable[Path]):
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        yield obj
        except OSError:
            continue


def structured_log_files(log_path: Path) -> list[Path]:
    base = log_path.with_name(log_path.stem + ".jsonl")
    # Oldest rotated file first, live file last, preserving temporal order.
    result = [Path(str(base) + f".{i}") for i in range(5, 0, -1)]
    result.append(base)
    return result


def load_activity_runs(log_path: Path, *, limit: int = 100) -> tuple[ActivityRun, ...]:
    buckets: dict[str, list[RunEvent]] = {}
    for obj in _iter_jsonl(structured_log_files(log_path)):
        run_id = obj.get("run_id")
        operation = obj.get("operation")
        phase = obj.get("run_phase")
        if not (run_id and operation and phase):
            continue
        event = RunEvent(
            timestamp=str(obj.get("timestamp", "")),
            run_id=str(run_id),
            operation=str(operation),
            phase=str(phase),
            outcome=obj.get("run_outcome"),
            message=str(obj.get("message", "")),
            elapsed_ms=obj.get("elapsed_ms") if isinstance(obj.get("elapsed_ms"), int) else None,
            detail=obj.get("run_detail") if isinstance(obj.get("run_detail"), dict) else None,
        )
        buckets.setdefault(event.run_id, []).append(event)

    runs: list[ActivityRun] = []
    for run_id, events in buckets.items():
        events.sort(key=lambda e: e.timestamp)
        first, last = events[0], events[-1]
        terminal = next((e for e in reversed(events) if e.phase == "end"), None)
        outcome = terminal.outcome if terminal and terminal.outcome else "running"
        runs.append(ActivityRun(
            run_id=run_id,
            operation=first.operation,
            started_at=first.timestamp,
            ended_at=terminal.timestamp if terminal else None,
            outcome=outcome,
            elapsed_ms=terminal.elapsed_ms if terminal else None,
            events=tuple(events),
        ))
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return tuple(runs[:max(0, limit)])


def get_activity_run(log_path: Path, run_id: str) -> ActivityRun:
    for run in load_activity_runs(log_path, limit=10000):
        if run.run_id == run_id:
            return run
    raise ValueError(f"Operational run not found: {run_id}")


def export_run_transcript(config, run_id: str, destination: Path) -> Path:
    """Export one sanitized run transcript as JSON; no DB/photo content."""
    from ppa.diagnostics import _redaction_pairs, sanitize_data

    run = get_activity_run(config.log_path, run_id)
    pairs = _redaction_pairs(config)
    payload = asdict(run)
    payload = sanitize_data(payload, pairs)
    envelope = {
        "schema": RUN_TRANSCRIPT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run": payload,
        "explicit_exclusions": [
            "catalogue database", "source photos", "thumbnails", "pilot session files"
        ],
    }
    destination = Path(destination)
    if destination.suffix.lower() != ".json":
        destination = destination.with_suffix(".json")
    from ppa.safe_export import safe_export_text
    return safe_export_text(
        destination, json.dumps(envelope, indent=2, sort_keys=True), config=config
    )


def concise_runs_text(runs: Iterable[ActivityRun]) -> str:
    lines = []
    for r in runs:
        dur = "" if r.elapsed_ms is None else f" {r.elapsed_ms / 1000:.1f}s"
        lines.append(f"{r.started_at}  {r.run_id}  {r.operation}  {r.outcome}{dur}")
    return "\n".join(lines)
