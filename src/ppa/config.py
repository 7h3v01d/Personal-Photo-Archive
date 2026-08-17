"""Configuration loading.

Deliberately minimal: a single TOML file, read into a plain dataclass.
No env-var overrides or CLI-flag merging yet — add that when a second
config source is actually needed, not preemptively.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "personal-photo-archive" / "config.toml"

_DEFAULT_TOML = """\
# Personal Photo Archive configuration

[database]
# Where the catalogue database lives. Not inside the photo library.
path = "~/.local/share/personal-photo-archive/catalogue.sqlite3"

[logging]
level = "INFO"
path = "~/.local/share/personal-photo-archive/logs/ppa.log"

[library]
# Referenced-library directories to scan (Phase 1). Empty until the user
# adds one via the UI/CLI.
directories = []
"""


@dataclass(frozen=True)
class Config:
    db_path: Path
    log_level: str
    log_path: Path
    library_directories: list[Path]

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Config":
        path = config_path or DEFAULT_CONFIG_PATH

        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_DEFAULT_TOML, encoding="utf-8")

        with path.open("rb") as f:
            raw = tomllib.load(f)

        db_path = Path(raw["database"]["path"]).expanduser()
        log_level = raw["logging"]["level"]
        log_path = Path(raw["logging"]["path"]).expanduser()
        library_directories = [
            Path(d).expanduser() for d in raw.get("library", {}).get("directories", [])
        ]

        return cls(
            db_path=db_path,
            log_level=log_level,
            log_path=log_path,
            library_directories=library_directories,
        )
