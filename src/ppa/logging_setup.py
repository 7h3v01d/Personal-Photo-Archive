"""Application logging setup.

PPA writes two rotating logs:
* ``ppa.log``: human-readable activity suitable for live monitoring.
* ``ppa.jsonl``: one structured JSON object per record for diagnostics/tools.

Both are operational records only; they are never archive evidence.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(log_path: Path, level: str = "INFO") -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("ppa")
    root.setLevel(level.upper())
    root.propagate = False
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT)
    text_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    text_handler.setFormatter(formatter)
    root.addHandler(text_handler)

    json_path = log_path.with_name(log_path.stem + ".jsonl")
    json_handler = RotatingFileHandler(
        json_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    json_handler.setFormatter(JsonLinesFormatter())
    root.addHandler(json_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ppa.{name}")
