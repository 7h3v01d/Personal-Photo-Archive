"""Full-size photo preview.

Opens the selected photograph at full size, scaled to fit the window, with a
caption (filename, dimensions, date, camera) and left/right navigation through
whatever is currently shown in the grid.

READ-ONLY: the preview loads the original file's bytes only to display them
(exactly as thumbnails already do). It never writes to a source photograph.
A file that is missing or unreadable shows a clear placeholder instead.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImageReader, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from ppa import catalogue
from ppa.ui import theme

# Keep a few recently-viewed decoded images so navigation is instant without
# holding the whole library in memory.
_CACHE_LIMIT = 6


def _caption(detail: catalogue.FileDetail) -> str:
    bits: list[str] = [detail.filename]
    if detail.width_px and detail.height_px:
        bits.append(f"{detail.width_px}×{detail.height_px}")
    date = next((v for (k, v) in detail.observed_metadata
                 if "date" in k.lower() or "taken" in k.lower()), None)
    if date:
        bits.append(date)
    if detail.camera:
        bits.append(detail.camera)
    if detail.copy_count > 1:
        bits.append(f"{detail.copy_count} copies")
    return "   ·   ".join(bits)


class PreviewDialog(QDialog):
    """Full-size viewer over the current grid contents, starting at ``start_row``."""

    def __init__(self, conn, model, start_row: int, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._model = model
        self._ids = [it.file_id for it in model._items]
        self._pos = max(0, min(start_row, len(self._ids) - 1))
        self._original: QPixmap | None = None
        self._cache: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._load_token = 0

        self.setWindowTitle("Preview")
        self.setModal(False)
        # Open at a comfortable fraction of the screen.
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(int(avail.width() * 0.72), int(avail.height() * 0.8))
            # Decode no larger than the screen; a 24MP photo need not become a
            # 24MP QPixmap just to be shown on a 2K display.
            self._decode_bound = avail.size() * (screen.devicePixelRatio() or 1.0)
        else:  # pragma: no cover - headless fallback
            self.resize(1000, 760)
            from PySide6.QtCore import QSize
            self._decode_bound = QSize(2560, 1600)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._image = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._image.setStyleSheet(f"background: {theme.OBSIDIAN};")
        self._image.setMinimumSize(1, 1)
        root.addWidget(self._image, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 8)
        self._prev = QPushButton("‹ Prev")
        self._prev.clicked.connect(self._go_prev)
        self._next = QPushButton("Next ›")
        self._next.clicked.connect(self._go_next)
        self._caption = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._caption.setStyleSheet(f"color: {theme.TEXT};")
        self._caption.setWordWrap(False)
        bar.addWidget(self._prev)
        bar.addWidget(self._caption, 1)
        bar.addWidget(self._next)
        root.addLayout(bar)

        self._load()

    # --- navigation ---------------------------------------------------------
    def _go_prev(self) -> None:
        if self._pos > 0:
            self._pos -= 1
            self._load()

    def _go_next(self) -> None:
        if self._pos < len(self._ids) - 1:
            self._pos += 1
            self._load()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self._go_prev()
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Space):
            self._go_next()
        else:
            super().keyPressEvent(event)

    # --- loading ------------------------------------------------------------
    def _load(self) -> None:
        if not self._ids:
            self._image.setText("No photo to preview.")
            self._caption.clear()
            return
        self._prev.setEnabled(self._pos > 0)
        self._next.setEnabled(self._pos < len(self._ids) - 1)

        detail = catalogue.file_detail(self._conn, self._ids[self._pos])
        counter = f"{self._pos + 1} / {len(self._ids)}"
        if detail is None:
            self._original = None
            self._image.setPixmap(QPixmap())
            self._image.setText("This photo is no longer catalogued.")
            self._caption.setText(counter)
            return

        self.setWindowTitle(f"Preview — {detail.filename}")
        self._caption.setText(f"{_caption(detail)}      ({counter})")

        file_id = detail.file_id
        cached = self._cache.get(file_id)
        if cached is not None:
            self._cache.move_to_end(file_id)
            self._show_pixmap(cached)
            return

        # Clear the previous image immediately and show a brief indicator, then
        # decode on the next event-loop turn so the indicator actually paints.
        self._original = None
        self._image.setPixmap(QPixmap())
        self._image.setStyleSheet(f"background: {theme.OBSIDIAN}; color: {theme.TEXT_DIM};")
        self._image.setText("Loading…")
        self._load_token += 1
        token = self._load_token
        QTimer.singleShot(0, lambda: self._decode(detail, token))

    def _decode(self, detail: catalogue.FileDetail, token: int) -> None:
        if token != self._load_token:
            return   # navigated away before this decode ran
        path = Path(detail.path)
        if not path.is_file():
            self._show_placeholder(
                f"“{detail.filename}” isn’t available right now.\n"
                "The file is catalogued but not reachable at its recorded location.")
            return
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)   # honour EXIF orientation (portrait upright)
        size = reader.size()
        if size.isValid() and (size.width() > self._decode_bound.width()
                               or size.height() > self._decode_bound.height()):
            reader.setScaledSize(size.scaled(
                self._decode_bound, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if token != self._load_token:
            return
        if image.isNull():
            self._show_placeholder(
                f"“{detail.filename}” could not be displayed.\n"
                "The file may be unreadable or an unsupported format.")
            return
        pix = QPixmap.fromImage(image)
        self._cache[detail.file_id] = pix
        self._cache.move_to_end(detail.file_id)
        while len(self._cache) > _CACHE_LIMIT:
            self._cache.popitem(last=False)
        self._show_pixmap(pix)

    def _show_pixmap(self, pix: QPixmap) -> None:
        self._original = pix
        self._image.setStyleSheet(f"background: {theme.OBSIDIAN};")
        self._image.setText("")
        self._rescale()

    def _show_placeholder(self, text: str) -> None:
        self._original = None
        self._image.setPixmap(QPixmap())
        self._image.setStyleSheet(f"background: {theme.OBSIDIAN}; color: {theme.TEXT_DIM};")
        self._image.setText(text)

    def _rescale(self) -> None:
        if self._original is None or self._original.isNull():
            return
        target = self._image.size()
        if target.width() < 2 or target.height() < 2:
            return
        self._image.setPixmap(self._original.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()
