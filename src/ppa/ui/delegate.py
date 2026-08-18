"""Grid tile delegate.

Draws each photo tile so the grid communicates catalogue state at a glance:

  * the thumbnail (or a placeholder), centred with a hairline frame
  * a status-coloured frame + a "MISSING" ribbon on missing files
  * an amber "xN" badge on files that share a Photo with others (duplicates)
  * a teal ring on the selected tile
  * an elided filename beneath

Kept purely presentational — it reads roles off the model and paints; it
owns no state.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from ppa.ui import theme
from ppa.ui.models import COPY_COUNT_ROLE, STATUS_ROLE

_PAD = 8
_LABEL_H = 22
_FRAME = 2


class PhotoTileDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect
        status = index.data(STATUS_ROLE) or "active"
        copy_count = index.data(COPY_COUNT_ROLE) or 1
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        # Tile background
        painter.fillRect(rect, QColor(theme.PANEL_ALT))

        # Image area (leave room for the label strip at the bottom)
        img_rect = QRect(
            rect.left() + _PAD,
            rect.top() + _PAD,
            rect.width() - 2 * _PAD,
            rect.height() - 2 * _PAD - _LABEL_H,
        )

        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(
                img_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = img_rect.left() + (img_rect.width() - scaled.width()) // 2
            y = img_rect.top() + (img_rect.height() - scaled.height()) // 2
            target = QRect(x, y, scaled.width(), scaled.height())
            painter.drawPixmap(target, scaled)
        else:
            painter.fillRect(img_rect, QColor(theme.PANEL))
            target = img_rect

        # Status frame: only draw for non-active states so a healthy grid
        # stays calm and problems stand out.
        if status != "active":
            pen = QPen(QColor(theme.status_colour(status)))
            pen.setWidth(_FRAME)
            painter.setPen(pen)
            painter.drawRect(img_rect.adjusted(1, 1, -1, -1))

        if status == "missing":
            # Dim the (placeholder) image and stamp a ribbon.
            painter.fillRect(img_rect, QColor(0, 0, 0, 130))
            self._ribbon(painter, img_rect, "MISSING", theme.RED)

        if copy_count and copy_count > 1:
            self._badge(painter, img_rect, f"x{copy_count}")

        # Selection ring
        if selected:
            pen = QPen(QColor(theme.TEAL))
            pen.setWidth(_FRAME)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))

        # Filename
        label_rect = QRect(
            rect.left() + 4,
            rect.bottom() - _LABEL_H,
            rect.width() - 8,
            _LABEL_H,
        )
        painter.setPen(QColor(theme.TEAL if selected else theme.TEXT))
        fm = QFontMetrics(painter.font())
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        elided = fm.elidedText(name, Qt.TextElideMode.ElideMiddle, label_rect.width())
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, elided)

        painter.restore()

    def _ribbon(self, painter: QPainter, area: QRect, text: str, colour: str) -> None:
        painter.save()
        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        w = fm.horizontalAdvance(text) + 16
        h = fm.height() + 4
        band = QRect(area.center().x() - w // 2, area.center().y() - h // 2, w, h)
        painter.fillRect(band, QColor(0, 0, 0, 180))
        painter.setPen(QColor(colour))
        painter.drawText(band, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def _badge(self, painter: QPainter, area: QRect, text: str) -> None:
        painter.save()
        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        w = fm.horizontalAdvance(text) + 12
        h = fm.height() + 2
        badge = QRect(area.right() - w - 4, area.top() + 4, w, h)
        painter.fillRect(badge, QColor(theme.OBSIDIAN))
        pen = QPen(QColor(theme.AMBER))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(badge)
        painter.setPen(QColor(theme.AMBER))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        return option.rect.size() if option.rect.isValid() else QSize(200, 220)
