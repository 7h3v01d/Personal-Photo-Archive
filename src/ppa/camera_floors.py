"""Camera manufacture floors — earliest plausible capture date for a model.

A photo dated before its camera model existed is impossible; that is independent
calendar evidence (a fact about the hardware, not its clock). We ship an EMPTY
default on purpose: a wrong floor would falsely condemn real dates, so a floor is
only used when explicitly provided. Unknown model -> no floor -> no conclusion.

Floors are conservative, day-granularity, and user-provided via a small JSON file
mapping "make|model" (case-insensitive) to a YYYY-MM-DD earliest date, e.g.:

    { "canon|powershot a70": "2003-01-01" }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _key(make: str | None, model: str | None) -> str:
    return f"{(make or '').strip().lower()}|{(model or '').strip().lower()}"


class CameraFloors:
    """Lookup of (make, model) -> earliest plausible capture datetime (UTC)."""

    def __init__(self, mapping: dict[str, datetime] | None = None):
        self._by_key = mapping or {}

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> "CameraFloors":
        out: dict[str, datetime] = {}
        for k, v in raw.items():
            make, _, model = k.partition("|")
            out[_key(make, model)] = datetime.strptime(v, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        return cls(out)

    @classmethod
    def load(cls, path: str | Path) -> "CameraFloors":
        p = Path(path)
        if not p.exists():
            return cls({})
        return cls.from_dict(json.loads(p.read_text()))

    def floor_for(self, make: str | None, model: str | None) -> datetime | None:
        return self._by_key.get(_key(make, model))

    def __len__(self) -> int:
        return len(self._by_key)
