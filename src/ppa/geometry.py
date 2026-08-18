"""Pure geometry helpers — no Qt, no I/O.

Kept separate from the Qt widgets so this logic can be imported and tested
in environments where PySide6 isn't installed (the previous placement inside
ui/gpsmap.py made the whole test suite fail to collect without Qt).
"""

from __future__ import annotations


def project(lat: float, lon: float, width: int, height: int) -> tuple[int, int]:
    """Equirectangular projection of (lat, lon) into pixel (x, y).

    lon -180..180 -> x 0..width, lat 90..-90 -> y 0..height. Values are
    clamped to the valid range so bad data can't paint off-canvas.
    """
    lat = max(-90.0, min(90.0, lat))
    lon = max(-180.0, min(180.0, lon))
    x = int(round((lon + 180.0) / 360.0 * width))
    y = int(round((90.0 - lat) / 180.0 * height))
    return x, y
