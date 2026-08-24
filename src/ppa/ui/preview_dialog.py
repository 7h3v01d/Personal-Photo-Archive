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

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QGuiApplication, QImageReader, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from ppa import catalogue
from ppa.ui import theme
from ppa.ui.workers import (
    BatchConfirmWorker, BatchPlanWorker, BatchSamplesWorker, EvidenceTraceWorker, WorkerRegistry,
)

# Keep a few recently-viewed decoded images so navigation is instant without
# holding the whole library in memory.
_CACHE_LIMIT = 6


_RELIABILITY_LABEL = {
    "TRUSTED": "trusted", "PROBABLY_VALID": "probably valid",
    "QUESTIONABLE": "questionable", "LIKELY_WRONG": "likely wrong", "UNKNOWN": "unknown",
}

# The reconstruction confidence enum includes "confirmed"/"proposed", which would
# collide with the review STATUS words in the caption; show clearer labels.
_CONFIDENCE_LABEL = {
    "confirmed": "exact", "strong": "strong", "range": "range", "proposed": "tentative",
}


def _stale_reason(rec) -> str:
    if rec.content_stale and rec.evidence_stale:
        return "photo and evidence changed"
    if rec.content_stale:
        return "photo changed"
    if rec.evidence_stale:
        return "evidence changed"
    return ""


def _date_line(summary) -> str:
    """A provenance-aware date line: what the camera recorded and how trustworthy,
    plus any interpreted (reconstructed) date with its review state and freshness."""
    parts: list[str] = []
    if summary.recorded is not None:
        rating = _RELIABILITY_LABEL.get(summary.recorded_reliability, "")
        parts.append(f"Recorded {summary.recorded.isoformat()}"
                     + (f" ({rating})" if rating else ""))
    else:
        parts.append("Recorded date unknown")

    rec = summary.reconstruction
    if rec is not None and rec.status != "rejected":
        span = (rec.start_date.isoformat() if rec.end_date is None
                else f"{rec.start_date.isoformat()}…{rec.end_date.isoformat()}")
        verb = {"confirmed": "Confirmed", "proposed": "Proposed"}.get(rec.status, rec.status)
        conf = _CONFIDENCE_LABEL.get(rec.confidence, rec.confidence)
        line = f"{verb} {span} ({conf})"
        reason = _stale_reason(rec)
        if reason:
            line += f" — STALE: {reason}"
        parts.append(line)
    return "      ·      ".join(parts)


def _caption(detail: catalogue.FileDetail, date_line: str) -> str:
    bits: list[str] = [detail.filename]
    if detail.width_px and detail.height_px:
        bits.append(f"{detail.width_px}×{detail.height_px}")
    if detail.camera:
        bits.append(detail.camera)
    if detail.copy_count > 1:
        bits.append(f"{detail.copy_count} copies")
    head = "   ·   ".join(bits)
    return f"{head}\n{date_line}" if date_line else head


class PreviewDialog(QDialog):
    """Full-size viewer over the current grid contents, starting at ``start_row``."""

    def __init__(self, conn, model, start_row: int, parent=None, *,
                 review_notes: dict[str, str] | None = None,
                 window_title: str = "Preview") -> None:
        super().__init__(parent)
        self._conn = conn
        self._model = model
        self._ids = [it.file_id for it in model._items]
        self._pos = max(0, min(start_row, len(self._ids) - 1))
        self._original: QPixmap | None = None
        self._cache: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._load_token = 0
        self._staleness = self._compute_staleness()
        self._review_notes = review_notes or {}
        self._window_title = window_title
        self._workers = WorkerRegistry()
        dbrow = conn.execute("PRAGMA database_list").fetchone()
        self._db_path = Path(dbrow["file"] if hasattr(dbrow, "keys") else dbrow[2])

        self.setWindowTitle(window_title)
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

        self._queue_note = QLabel("")
        self._queue_note.setWordWrap(True)
        self._queue_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._queue_note.setStyleSheet(f"color: {theme.AMBER}; padding: 6px 12px;")
        self._queue_note.hide()
        root.addWidget(self._queue_note)

        # Review row — confirm/reject/reopen the reconstruction for this photo.
        review = QHBoxLayout()
        review.setContentsMargins(10, 8, 10, 0)
        review.addStretch(1)
        self._review_status = QLabel("")
        self._review_status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        review.addWidget(self._review_status)
        self._confirm_btn = QPushButton("Confirm date")
        self._confirm_btn.clicked.connect(lambda: self._decide("confirm"))
        self._reject_btn = QPushButton("Reject")
        self._reject_btn.clicked.connect(lambda: self._decide("reject"))
        self._reopen_btn = QPushButton("Reopen")
        self._reopen_btn.clicked.connect(self._reopen)
        self._refresh_btn = QPushButton("Refresh proposal")
        self._refresh_btn.clicked.connect(self._refresh)
        self._why_btn = QPushButton("Why?")
        self._why_btn.setToolTip("Inspect the evidence and reasoning behind this date")
        self._why_btn.clicked.connect(self._show_why)
        self._batch_btn = QPushButton("Review batch…")
        self._batch_btn.setToolTip("Review a strictly eligible reconstructed reset run as one batch")
        self._batch_btn.clicked.connect(self._prepare_batch)
        for b in (self._confirm_btn, self._reject_btn, self._reopen_btn, self._refresh_btn,
                  self._why_btn, self._batch_btn):
            review.addWidget(b)
        review.addStretch(1)
        root.addLayout(review)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 8)
        self._prev = QPushButton("‹ Prev")
        self._prev.clicked.connect(self._go_prev)
        self._next = QPushButton("Next ›")
        self._next.clicked.connect(self._go_next)
        self._caption = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._caption.setStyleSheet(f"color: {theme.TEXT};")
        self._caption.setWordWrap(True)
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

    # --- review -------------------------------------------------------------
    def _compute_staleness(self):
        # One library-wide pass, cached for the session and refreshed after any
        # action, so navigation stays cheap while the UI still tells the same
        # freshness truth as the persistence layer. Skipped when nothing is stored.
        from ppa.reconstruct_catalogue import evaluate_staleness
        if self._conn.execute("SELECT 1 FROM reconstructions LIMIT 1").fetchone() is None:
            return {}
        return evaluate_staleness(self._conn)

    def _current_file_id(self) -> str | None:
        return self._ids[self._pos] if self._ids else None

    def _summary(self, file_id: str):
        from ppa.reconstruct_catalogue import file_date_summary
        return file_date_summary(self._conn, file_id, staleness=self._staleness)

    def _sync_review(self, summary) -> None:
        rec = summary.reconstruction
        if rec is None:
            for b in (self._confirm_btn, self._reject_btn, self._reopen_btn,
                      self._refresh_btn, self._batch_btn):
                b.setVisible(False)
            self._review_status.setText("")
            return

        stale = rec.stale
        reason = _stale_reason(rec)
        proposed = rec.status == "proposed"
        decided = rec.status in ("confirmed", "rejected")

        # A stale row is never actionable as if fresh: offer Refresh (proposed) or
        # Reopen & refresh (decided) instead of Confirm/Reject.
        self._confirm_btn.setVisible(proposed and not stale)
        self._reject_btn.setVisible(proposed and not stale)
        self._refresh_btn.setVisible(proposed and stale)
        self._reopen_btn.setVisible(decided)
        self._reopen_btn.setText("Reopen && refresh" if (decided and stale) else "Reopen")
        # Batch planning is itself strict and revalidates everything. Showing the
        # entry point only on fresh offset proposals keeps ordinary review uncluttered.
        self._batch_btn.setVisible(proposed and not stale and rec.method == "offset")

        if proposed and stale:
            self._review_status.setText(f"Proposed but STALE ({reason}) — refresh to review:")
        elif proposed:
            self._review_status.setText("Reconstruction proposed — review:")
        elif rec.status == "confirmed":
            self._review_status.setText(
                f"Confirmed — STALE: {reason}" if stale else "Date confirmed")
        elif rec.status == "rejected":
            self._review_status.setText(
                f"Rejected — STALE: {reason}" if stale else "Reconstruction rejected")

    def _after_action(self) -> None:
        self._staleness = self._compute_staleness()
        self._load()

    def _decide(self, which: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        from ppa.reconstruct_catalogue import confirm_reconstruction, reject_reconstruction
        fid = self._current_file_id()
        if fid is None:
            return
        fn = confirm_reconstruction if which == "confirm" else reject_reconstruction
        try:
            fn(self._conn, fid)
        except ValueError as exc:
            QMessageBox.information(self, "Review", str(exc))
        self._after_action()

    def _refresh(self) -> None:
        # Recompute proposals to today's bytes+evidence so the row is fresh to review.
        from ppa.reconstruct_catalogue import store_reconstructions
        store_reconstructions(self._conn)
        self._after_action()

    def _reopen(self) -> None:
        # Reopen returns a decision to 'proposed'; immediately recompute so the
        # user never reviews a stale row that merely looks fresh.
        from ppa.reconstruct_catalogue import reopen_reconstruction, store_reconstructions
        fid = self._current_file_id()
        if fid is not None:
            reopen_reconstruction(self._conn, fid)
            store_reconstructions(self._conn)
            self._after_action()

    def _prepare_batch(self) -> None:
        fid = self._current_file_id()
        if fid is None:
            return
        progress = QProgressDialog("Checking batch eligibility…", "", 0, 0, self)
        progress.setWindowTitle("Controlled batch review")
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        self._batch_progress = progress
        worker = BatchPlanWorker(self._db_path, fid)
        worker.finished.connect(self._on_batch_plan_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_batch_failed, Qt.ConnectionType.QueuedConnection)
        self._workers.start(worker)

    def _finish_batch_progress(self) -> None:
        progress = getattr(self, "_batch_progress", None)
        if progress is not None:
            progress.close(); progress.deleteLater(); self._batch_progress = None

    @Slot(object)
    def _on_batch_plan_ready(self, plan) -> None:
        self._finish_batch_progress()
        if plan is None:
            QMessageBox.information(
                self, "Controlled batch review",
                "This reconstruction is not currently eligible for batch confirmation. "
                "Batch review requires a complete fresh point-date run, strong single-device "
                "identity, and one exact human-anchor basis.")
            return
        progress = QProgressDialog("Preparing visual samples…", "", 0, 0, self)
        progress.setWindowTitle("Controlled batch review")
        progress.setCancelButton(None); progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.show()
        self._batch_progress = progress
        self._pending_batch_plan = plan
        worker = BatchSamplesWorker(self._db_path, plan)
        worker.finished.connect(self._on_batch_samples_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_batch_failed, Qt.ConnectionType.QueuedConnection)
        self._workers.start(worker)

    @Slot(object)
    def _on_batch_samples_ready(self, images) -> None:
        plan = getattr(self, "_pending_batch_plan", None)
        self._pending_batch_plan = None
        self._finish_batch_progress()
        if plan is not None:
            self._show_batch_samples(plan, dict(images))

    def _show_batch_samples(self, plan, images) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Controlled batch review — {plan.member_count} photos")
        dialog.resize(1050, 650)
        root = QVBoxLayout(dialog)
        intro = QLabel(
            f"Review these distributed samples before confirming all {plan.member_count} photos.\n"
            f"Clock offset: {plan.day_offset:+d} days.  No source photo or EXIF will be changed.\n"
            f"{plan.reason}")
        intro.setWordWrap(True); root.addWidget(intro)
        grid = QGridLayout(); root.addLayout(grid, 1)
        by_id = {m.file_id: m for m in plan.members}
        for col, fid in enumerate(plan.sample_file_ids):
            m = by_id[fid]
            card = QVBoxLayout()
            image = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
            image.setMinimumSize(170, 130); image.setStyleSheet(f"background: {theme.OBSIDIAN};")
            qimg = images.get(fid)
            if qimg is not None and not qimg.isNull():
                image.setPixmap(QPixmap.fromImage(qimg))
            else:
                image.setText("Unavailable / unreadable")
            text = QLabel(f"{m.filename}\n→ {m.start_date}\n{m.method} / {m.confidence}")
            text.setAlignment(Qt.AlignmentFlag.AlignCenter); text.setWordWrap(True)
            card.addWidget(image); card.addWidget(text)
            holder = QVBoxLayout(); holder.addLayout(card)
            grid.addLayout(holder, 0, col)
        ack = QCheckBox(
            f"I reviewed the distributed samples and want to confirm all {plan.member_count} proposed dates.")
        root.addWidget(ack)
        row = QHBoxLayout(); row.addStretch(1)
        cancel = QPushButton("Cancel"); confirm = QPushButton(f"Confirm all {plan.member_count}")
        confirm.setEnabled(False); ack.toggled.connect(confirm.setEnabled)
        cancel.clicked.connect(dialog.reject); confirm.clicked.connect(dialog.accept)
        row.addWidget(cancel); row.addWidget(confirm); root.addLayout(row)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._commit_batch(plan)

    def _commit_batch(self, plan) -> None:
        progress = QProgressDialog("Revalidating and confirming batch…", "", 0, 0, self)
        progress.setWindowTitle("Controlled batch confirmation")
        progress.setCancelButton(None); progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.show()
        self._batch_progress = progress
        worker = BatchConfirmWorker(self._db_path, plan)
        worker.finished.connect(self._on_batch_confirmed, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_batch_failed, Qt.ConnectionType.QueuedConnection)
        self._workers.start(worker)

    @Slot(int)
    def _on_batch_confirmed(self, count: int) -> None:
        self._finish_batch_progress()
        QMessageBox.information(self, "Batch confirmed",
                                f"Confirmed {count} reconstruction(s). Each decision remains "
                                "individually provenance-bound to its reviewed bytes and evidence.")
        self._after_action()

    @Slot(str)
    def _on_batch_failed(self, message: str) -> None:
        self._finish_batch_progress()
        QMessageBox.warning(self, "Controlled batch review", message)
        self._after_action()

    def _show_why(self) -> None:
        fid = self._current_file_id()
        if fid is None:
            return
        progress = QProgressDialog("Building evidence trace…", "", 0, 0, self)
        progress.setWindowTitle("Why this date?")
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        self._why_progress = progress

        worker = EvidenceTraceWorker(self._db_path, fid)
        worker.progress.connect(self._on_why_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_why_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_why_failed, Qt.ConnectionType.QueuedConnection)
        self._workers.start(worker)

    @Slot(str)
    def _on_why_progress(self, message: str) -> None:
        progress = getattr(self, "_why_progress", None)
        if progress is not None:
            progress.setLabelText(message)

    def _finish_why_progress(self) -> None:
        progress = getattr(self, "_why_progress", None)
        if progress is not None:
            progress.close()
            progress.deleteLater()
            self._why_progress = None

    @Slot(object)
    def _on_why_ready(self, trace) -> None:
        from ppa.evidence_inspector import concise_text
        self._finish_why_progress()
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Why this date? — {trace.filename}")
        dialog.resize(780, 650)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(concise_text(trace))
        layout.addWidget(text, 1)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(close)
        layout.addLayout(row)
        dialog.exec()

    @Slot(str)
    def _on_why_failed(self, message: str) -> None:
        self._finish_why_progress()
        QMessageBox.warning(self, "Evidence Inspector", f"Could not build evidence trace: {message}")

    # --- loading ------------------------------------------------------------
    def _load(self) -> None:
        if not self._ids:
            self._image.setText("No photo to preview.")
            self._caption.clear()
            return
        self._prev.setEnabled(self._pos > 0)
        self._next.setEnabled(self._pos < len(self._ids) - 1)

        fid = self._ids[self._pos]
        note = self._review_notes.get(fid, "")
        self._queue_note.setText(note)
        self._queue_note.setVisible(bool(note))
        detail = catalogue.file_detail(self._conn, fid)
        counter = f"{self._pos + 1} / {len(self._ids)}"
        if detail is None:
            self._original = None
            self._image.setPixmap(QPixmap())
            self._image.setText("This photo is no longer catalogued.")
            self._caption.setText(counter)
            return

        self.setWindowTitle(f"{self._window_title} — {detail.filename}")
        summary = self._summary(detail.file_id)
        self._caption.setText(f"{_caption(detail, _date_line(summary))}"
                              f"      ({counter})")
        self._sync_review(summary)

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

    def closeEvent(self, event) -> None:
        self._workers.shutdown()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()


# Evidence-inspector workers are owned by each PreviewDialog and are shut down
# with the dialog; source-photo decoding remains read-only.
