"""GPS mini-map (offline, schematic).

Local-first means no cloud map tiles. Instead of a slippy map, this draws a
clean equirectangular grid — equator and prime meridian emphasised — and
plots the photo's coordinate as a teal pin, with the decimal readout beneath.
It's honest about being a schematic locator rather than a street map, and it
works with no network at all.

The projection is a pure function so it can be unit-tested without Qt.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ppa.geometry import project
from ppa.ui import theme


class GpsMiniMap(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._lat: float | None = None
        self._lon: float | None = None
        self.setMinimumHeight(120)
        self.setMaximumHeight(150)

    def set_coords(self, lat: float, lon: float) -> None:
        self._lat, self._lon = lat, lon
        self.update()

    def clear(self) -> None:
        self._lat = self._lon = None
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        readout_h = 20
        map_rect = QRect(0, 0, self.width() - 1, self.height() - readout_h - 1)

        p.fillRect(map_rect, QColor(theme.OBSIDIAN))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawRect(map_rect)

        # Graticule every 30deg lon / 30deg lat
        faint = QPen(QColor(theme.BORDER), 1)
        p.setPen(faint)
        for lon in range(-150, 180, 30):
            x, _ = project(0, lon, map_rect.width(), map_rect.height())
            p.drawLine(map_rect.left() + x, map_rect.top(),
                       map_rect.left() + x, map_rect.bottom())
        for lat in range(-60, 90, 30):
            _, y = project(lat, 0, map_rect.width(), map_rect.height())
            p.drawLine(map_rect.left(), map_rect.top() + y,
                       map_rect.right(), map_rect.top() + y)

        # Emphasise equator + prime meridian
        p.setPen(QPen(QColor(theme.TEXT_DIM), 1))
        _, ey = project(0, 0, map_rect.width(), map_rect.height())
        ex, _ = project(0, 0, map_rect.width(), map_rect.height())
        p.drawLine(map_rect.left(), map_rect.top() + ey, map_rect.right(), map_rect.top() + ey)
        p.drawLine(map_rect.left() + ex, map_rect.top(), map_rect.left() + ex, map_rect.bottom())

        readout_rect = QRect(0, map_rect.bottom() + 1, self.width(), readout_h)

        if self._lat is None or self._lon is None:
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(readout_rect, Qt.AlignmentFlag.AlignCenter, "no location")
            p.end()
            return

        # Plot the pin
        x, y = project(self._lat, self._lon, map_rect.width(), map_rect.height())
        cx, cy = map_rect.left() + x, map_rect.top() + y
        p.setPen(QPen(QColor(theme.TEAL), 1))
        p.drawLine(cx - 6, cy, cx + 6, cy)
        p.drawLine(cx, cy - 6, cx, cy + 6)
        p.setBrush(QColor(theme.TEAL))
        p.drawEllipse(cx - 3, cy - 3, 6, 6)

        p.setPen(QColor(theme.TEAL))
        font = QFont(p.font())
        p.setFont(font)
        p.drawText(readout_rect, Qt.AlignmentFlag.AlignCenter,
                   f"{self._lat:.5f}, {self._lon:.5f}")
        p.end()
