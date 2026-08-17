"""Logging setup.

Every scan/import/analysis action should be reconstructable from the log
(Phase 0 rule: "analysis must be reproducible where practical"), so this
uses a structured, timestamped format from day one rather than bare print()
statements that get replaced later.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(log_path: Path, level: str = "INFO") -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("ppa")
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter(_FORMAT)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ppa.{name}")
