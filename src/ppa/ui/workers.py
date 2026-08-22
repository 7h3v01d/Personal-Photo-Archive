"""Background workers (QObject moved onto a QThread).

The scanner and verifier can run for a long time over a 10,000-photo
library, and thumbnailing decodes images off disk — none of that may block
the GUI thread. Each of these is a QObject with its work in a @Slot, moved
onto its own QThread. The caller is responsible for holding a reference to
both the worker and the thread until the work finishes (otherwise Python GC
can collect a live QThread mid-run); WorkerHandle + WorkerRegistry below
make that bookkeeping explicit.

SQLite connections are not shareable across threads, so every worker that
touches the catalogue opens its **own** connection on its own thread from
the db path — it never borrows the GUI thread's connection.
"""

from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtGui import QImage

from ppa.db import connect
from ppa.integrity import verify_library
from ppa.metadata import extract_stale
from ppa.scanner import scan_library
from ppa.thumbnails import ThumbnailCache


class ScanWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # ScanReport
    failed = Signal(str)

    def __init__(self, db_path: Path, library_path: Path,
                 protected_paths: list[Path] | None = None) -> None:
        super().__init__()
        self._db_path = db_path
        self._library_path = library_path
        self._protected_paths = protected_paths

    @Slot()
    def run(self) -> None:
        try:
            conn = connect(self._db_path)  # own connection, this thread
            report = scan_library(
                conn, self._library_path,
                progress_cb=self.progress.emit,
                protected_paths=self._protected_paths,
            )
            conn.close()
            self.finished.emit(report)
        except Exception as exc:  # surfaced to the UI, never swallowed
            self.failed.emit(str(exc))


class VerifyWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # VerifyReport
    failed = Signal(str)

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path

    @Slot()
    def run(self) -> None:
        try:
            conn = connect(self._db_path)
            report = verify_library(conn, progress_cb=self.progress.emit)
            conn.close()
            self.finished.emit(report)
        except Exception as exc:
            self.failed.emit(str(exc))


class MetadataWorker(QObject):
    progress = Signal(str)
    finished = Signal(int)  # number of files processed
    failed = Signal(str)

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path

    @Slot()
    def run(self) -> None:
        try:
            conn = connect(self._db_path)
            count = extract_stale(conn, progress_cb=self.progress.emit)
            conn.close()
            self.finished.emit(count)
        except Exception as exc:
            self.failed.emit(str(exc))


class DateReviewQueueWorker(QObject):
    """Build the Phase-7 review queue without blocking the GUI thread.

    The worker owns its SQLite connection. Cancellation is cooperative: the UI
    can set a thread-safe event immediately; the analytics layer checks it at
    stage boundaries and inside its collection loops.  A long pure chronology
    stage cannot be interrupted mid-function, but the GUI always remains live.
    """

    progress = Signal(str)
    finished = Signal(object)  # ReviewQueue
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__()
        self._db_path = db_path
        self._library_id = library_id
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.pilot import PilotAnalysisCancelled
            from ppa.review_queue import build_review_queue

            self.progress.emit("Date Review: analysing chronology…")
            conn = connect(self._db_path)
            queue = build_review_queue(
                conn,
                library_id=self._library_id,
                progress_cb=self.progress.emit,
                cancel_cb=self._cancel.is_set,
            )
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                self.finished.emit(queue)
        except PilotAnalysisCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class EvidenceTraceWorker(QObject):
    """Build one Phase-7 evidence trace off the GUI thread."""

    progress = Signal(str)
    finished = Signal(object)  # EvidenceTrace
    failed = Signal(str)

    def __init__(self, db_path: Path, file_id: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._file_id = file_id

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.evidence_inspector import inspect_date_evidence
            self.progress.emit("Building evidence trace…")
            conn = connect(self._db_path)
            trace = inspect_date_evidence(conn, self._file_id)
            self.finished.emit(trace)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class BatchPlanWorker(QObject):
    """Prepare a controlled batch-confirmation plan off the GUI thread."""

    finished = Signal(object)  # BatchPlan | None
    failed = Signal(str)

    def __init__(self, db_path: Path, file_id: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._file_id = file_id

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.batch_review import plan_batch_confirmation
            conn = connect(self._db_path)
            self.finished.emit(plan_batch_confirmation(conn, self._file_id))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class BatchSamplesWorker(QObject):
    """Decode the bounded visual spot-check set off the GUI thread."""

    finished = Signal(object)  # tuple[(file_id, QImage | None), ...]
    failed = Signal(str)

    def __init__(self, db_path: Path, plan, width: int = 220, height: int = 170) -> None:
        super().__init__()
        self._db_path = db_path
        self._plan = plan
        self._width = width
        self._height = height

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from PySide6.QtCore import QSize
            from PySide6.QtGui import QImageReader
            from ppa import catalogue
            conn = connect(self._db_path)
            out = []
            for fid in self._plan.sample_file_ids:
                detail = catalogue.file_detail(conn, fid)
                img = None
                if detail is not None and Path(detail.path).is_file():
                    reader = QImageReader(detail.path)
                    reader.setAutoTransform(True)
                    reader.setScaledSize(QSize(self._width, self._height))
                    candidate = reader.read()
                    if not candidate.isNull():
                        img = candidate
                out.append((fid, img))
            self.finished.emit(tuple(out))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class BatchConfirmWorker(QObject):
    """Atomically revalidate and commit an already-reviewed batch plan."""

    finished = Signal(int)
    failed = Signal(str)

    def __init__(self, db_path: Path, plan) -> None:
        super().__init__()
        self._db_path = db_path
        self._plan = plan

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.batch_review import confirm_batch
            conn = connect(self._db_path)
            self.finished.emit(confirm_batch(conn, self._plan))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class ThumbnailWorker(QObject):
    """Long-lived worker that services thumbnail requests off a queue.

    request(file_id, path, sha256) is a queued slot; as each thumbnail is
    rendered (via the disk-cached ThumbnailCache) it emits ready(file_id,
    QImage). QImage is safe to build off the GUI thread; the receiver turns
    it into a QPixmap on the GUI side.
    """

    ready = Signal(str, QImage)

    def __init__(self, cache_dir: Path, size: int = 256) -> None:
        super().__init__()
        self._cache = ThumbnailCache(cache_dir, size=size)

    @Slot(str, str, str)
    def request(self, file_id: str, path: str, sha256: str) -> None:
        sha = sha256 or None
        out = self._cache.get_or_create(Path(path), sha)
        if out is None:
            return
        img = QImage(str(out))
        if not img.isNull():
            self.ready.emit(file_id, img)


class WorkerHandle:
    """Owns a (worker, thread) pair and keeps both alive until done."""

    def __init__(self, worker: QObject, thread: QThread) -> None:
        self.worker = worker
        self.thread = thread


class WorkerRegistry:
    """Keeps strong references to in-flight workers/threads (GC-safety) and
    tears them down cleanly when they finish. This is the explicit form of
    the usual ``self._worker_refs`` list.
    """

    def __init__(self) -> None:
        self._handles: list[WorkerHandle] = []

    def start(self, worker: QObject, *, run_slot: str = "run") -> WorkerHandle:
        thread = QThread()
        worker.moveToThread(thread)
        handle = WorkerHandle(worker, thread)
        self._handles.append(handle)

        thread.started.connect(getattr(worker, run_slot))

        # When the worker signals completion, quit the thread and drop refs.
        for sig_name in ("finished", "failed", "cancelled"):
            sig = getattr(worker, sig_name, None)
            if sig is not None:
                sig.connect(lambda *_, h=handle: self._retire(h))

        thread.start()
        return handle

    def start_persistent(self, worker: QObject) -> WorkerHandle:
        """Start a worker with no run slot (it reacts to queued signals) and
        keep it alive for the app's lifetime.
        """
        thread = QThread()
        worker.moveToThread(thread)
        handle = WorkerHandle(worker, thread)
        self._handles.append(handle)
        thread.start()
        return handle

    def _retire(self, handle: WorkerHandle) -> None:
        handle.thread.quit()
        handle.thread.wait()
        if handle in self._handles:
            self._handles.remove(handle)

    def shutdown(self) -> None:
        for handle in list(self._handles):
            handle.thread.quit()
            handle.thread.wait()
        self._handles.clear()
