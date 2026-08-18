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

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtGui import QImage

from ppa.db import connect
from ppa.integrity import verify_library
from ppa.scanner import scan_library
from ppa.thumbnails import ThumbnailCache


class ScanWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # ScanReport
    failed = Signal(str)

    def __init__(self, db_path: Path, library_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self._library_path = library_path

    @Slot()
    def run(self) -> None:
        try:
            conn = connect(self._db_path)  # own connection, this thread
            report = scan_library(conn, self._library_path, progress_cb=self.progress.emit)
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

    @Slot(str, str, object)
    def request(self, file_id: str, path: str, sha256: object) -> None:
        sha = sha256 if isinstance(sha256, str) else None
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
        for sig_name in ("finished", "failed"):
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
