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

from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt, QMetaObject

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:  # pragma: no cover - supplied with PySide6
    def _qt_object_is_valid(obj):
        return obj is not None
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
        conn = None
        try:
            conn = connect(self._db_path)  # own connection, this thread
            report = scan_library(
                conn, self._library_path,
                progress_cb=self.progress.emit,
                protected_paths=self._protected_paths,
            )
            self.finished.emit(report)
        except Exception as exc:  # surfaced to the UI, never swallowed
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class VerifyWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # VerifyReport
    failed = Signal(str)

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            conn = connect(self._db_path)
            report = verify_library(conn, progress_cb=self.progress.emit)
            self.finished.emit(report)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class MetadataWorker(QObject):
    progress = Signal(str)
    finished = Signal(int)  # number of files processed
    failed = Signal(str)

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            conn = connect(self._db_path)
            count = extract_stale(conn, progress_cb=self.progress.emit)
            self.finished.emit(count)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


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

    def __init__(self, db_path: Path, library_id: int, *, directory_prefix: str | None = None, file_ids=None) -> None:
        super().__init__()
        self._db_path = db_path
        self._library_id = library_id
        self._directory_prefix = directory_prefix
        self._file_ids = tuple(file_ids) if file_ids is not None else None
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
                directory_prefix=self._directory_prefix, file_ids=self._file_ids,
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


class UnresolvedMemoriesWorker(QObject):
    """Build the Phase-7.2.6 unresolved-memory view off the GUI thread."""

    progress = Signal(str)
    finished = Signal(object)  # UnresolvedMemories
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, db_path: Path, library_id: int, *, directory_prefix: str | None = None, file_ids=None) -> None:
        super().__init__()
        self._db_path = db_path
        self._library_id = library_id
        self._directory_prefix = directory_prefix
        self._file_ids = tuple(file_ids) if file_ids is not None else None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.pilot import PilotAnalysisCancelled
            from ppa.unresolved import build_unresolved_memories
            conn = connect(self._db_path)
            view = build_unresolved_memories(
                conn, library_id=self._library_id,
                directory_prefix=self._directory_prefix, file_ids=self._file_ids,
                progress_cb=self.progress.emit, cancel_cb=self._cancel.is_set)
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                self.finished.emit(view)
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


class OrganizationSuggestionsWorker(QObject):
    """Build Phase-9.9 conservative assisted-curation suggestions off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_suggestions import build_organization_suggestions
            conn=connect(self._db_path)
            self.finished.emit(build_organization_suggestions(conn,library_id=self._library_id))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationSuggestionBrowseWorker(QObject):
    """Build the logical-Photo review view for one suggestion off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, suggestion) -> None:
        super().__init__(); self._db_path=db_path; self._suggestion=suggestion
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_suggestions import build_suggestion_browse
            conn=connect(self._db_path)
            self.finished.emit(build_suggestion_browse(conn,self._suggestion))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationSuggestionApplyWorker(QObject):
    """Apply one explicitly approved, freshly revalidated suggestion."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, suggestion) -> None:
        super().__init__(); self._db_path=db_path; self._suggestion=suggestion
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_suggestions import apply_organization_suggestion
            conn=connect(self._db_path)
            self.finished.emit(apply_organization_suggestion(conn,self._suggestion))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationSuggestionDismissWorker(QObject):
    """Dismiss one freshly revalidated suggestion and persist review state."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, suggestion, note: str | None = None) -> None:
        super().__init__(); self._db_path=db_path; self._suggestion=suggestion; self._note=note
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_suggestions import dismiss_organization_suggestion
            conn=connect(self._db_path)
            self.finished.emit(dismiss_organization_suggestion(conn,self._suggestion,note=self._note))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationSuggestionReviewsWorker(QObject):
    """Load durable suggestion review state off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_suggestions import list_suggestion_reviews
            conn=connect(self._db_path)
            self.finished.emit(list_suggestion_reviews(conn,library_id=self._library_id))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationSuggestionRestoreWorker(QObject):
    """Restore one dismissed suggestion fingerprint."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int, suggestion_id: str, note: str | None = None) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id; self._suggestion_id=suggestion_id; self._note=note
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_suggestions import restore_organization_suggestion
            conn=connect(self._db_path)
            self.finished.emit(restore_organization_suggestion(conn,library_id=self._library_id,suggestion_id=self._suggestion_id,note=self._note))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class ArchiveHealthWorker(QObject):
    """Build Phase-12.1 Backup & Archive Health off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.archive_health import build_archive_health
            conn=connect(self._db_path)
            self.finished.emit(build_archive_health(conn, library_id=self._library_id))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class ArchiveHealthBrowseWorker(QObject):
    """Build one read-only Phase-12.1 health-category browser off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, health, category: str) -> None:
        super().__init__(); self._db_path=db_path; self._health=health; self._category=category
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.archive_health import build_archive_health_browse
            conn=connect(self._db_path)
            self.finished.emit(build_archive_health_browse(conn, self._health, self._category))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationHealthWorker(QObject):
    """Build Phase-9.8 organisation health off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_health import build_organization_health
            conn=connect(self._db_path)
            self.finished.emit(build_organization_health(conn, library_id=self._library_id))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationGapWorker(QObject):
    """Build one read-only organisation curation-gap browser off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, health, gap: str) -> None:
        super().__init__(); self._db_path=db_path; self._health=health; self._gap=gap
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_health import build_gap_browse
            conn=connect(self._db_path)
            self.finished.emit(build_gap_browse(conn, self._health, self._gap))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class TagHomeWorker(QObject):
    """Build Phase-9.5 Tag Home projection off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.tag_home import build_tag_home
            conn=connect(self._db_path)
            self.finished.emit(build_tag_home(conn, library_id=self._library_id))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationDiscoveryHomeWorker(QObject):
    """Build Album + Tag selector homes off-thread for Phase 9.6."""
    finished = Signal(object, object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.album_home import build_album_home
            from ppa.tag_home import build_tag_home
            conn=connect(self._db_path)
            self.finished.emit(build_album_home(conn,library_id=self._library_id), build_tag_home(conn,library_id=self._library_id))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationDiscoveryRunWorker(QObject):
    """Evaluate one explicit Album/Tag intersection off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int, album_ids, tag_ids) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id; self._album_ids=tuple(album_ids); self._tag_ids=tuple(tag_ids)
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_discovery import build_organization_discovery
            conn=connect(self._db_path)
            self.finished.emit(build_organization_discovery(conn,library_id=self._library_id,album_ids=self._album_ids,tag_ids=self._tag_ids))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class AlbumHomeWorker(QObject):
    """Build Phase-9.4 Album Home projection off-thread."""
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__()
        self._db_path = db_path
        self._library_id = library_id

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.album_home import build_album_home
            conn = connect(self._db_path)
            self.finished.emit(build_album_home(conn, library_id=self._library_id))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class EventHomeWorker(QObject):
    """Build Phase-8.9 Timeline + Family History projection off-thread."""
    progress = Signal(str)
    finished = Signal(object, object, object, object)  # TimelineView, EventHomeView, EventSearchIndex, EventHealthView
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
            from ppa.event_home import build_event_home
            from ppa.event_search import build_event_search_index
            from ppa.timeline import build_timeline
            self.progress.emit("Family History: building chronology projection…")
            conn = connect(self._db_path)
            view = build_timeline(conn, library_id=self._library_id)
            if self._cancel.is_set():
                self.cancelled.emit(); return
            self.progress.emit("Family History: assembling Event cards…")
            home = build_event_home(conn, view)
            if self._cancel.is_set():
                self.cancelled.emit(); return
            self.progress.emit("Family History: building search index…")
            search_index = build_event_search_index(conn, home)
            if self._cancel.is_set():
                self.cancelled.emit(); return
            self.progress.emit("Family History: evaluating curation health…")
            from ppa.event_health import build_event_health_view
            health = build_event_health_view(conn, view)
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                self.finished.emit(view, home, search_index, health)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class WorkerHandle:
    """Owns a (worker, thread) pair and keeps both alive until done."""

    def __init__(self, worker: QObject, thread: QThread) -> None:
        self.worker = worker
        self.thread = thread


class WorkerRegistry(QObject):
    """Own background workers and retire them on the registry's QObject thread.

    Terminal worker signals only request ``QThread.quit``.  Actual cleanup is
    performed from the decorated ``thread.finished`` receiver below.  This
    deliberately avoids un-affined Python lambdas running in a worker context
    and potentially asking a QThread to wait for itself.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handles: list[WorkerHandle] = []

    def start(self, worker: QObject, *, run_slot: str = "run") -> WorkerHandle:
        thread = QThread()
        worker.moveToThread(thread)
        handle = WorkerHandle(worker, thread)
        self._handles.append(handle)

        thread.started.connect(getattr(worker, run_slot), Qt.ConnectionType.QueuedConnection)

        # Every terminal path requests a normal QThread event-loop exit.  The
        # thread.finished signal is emitted after the worker thread has stopped;
        # the registry then drops strong references on its own QObject thread.
        for sig_name in ("finished", "failed", "cancelled"):
            sig = getattr(worker, sig_name, None)
            if sig is not None:
                # Standard worker-object lifecycle: schedule deletion on the
                # worker's own thread, then request that thread to exit.
                sig.connect(worker.deleteLater)
                sig.connect(thread.quit, Qt.ConnectionType.QueuedConnection)
        thread.finished.connect(self._on_thread_finished, Qt.ConnectionType.QueuedConnection)

        thread.start()
        return handle

    def start_persistent(self, worker: QObject) -> WorkerHandle:
        """Start a worker with no run slot and keep it until shutdown."""
        thread = QThread()
        worker.moveToThread(thread)
        handle = WorkerHandle(worker, thread)
        self._handles.append(handle)
        thread.finished.connect(self._on_thread_finished, Qt.ConnectionType.QueuedConnection)
        thread.start()
        return handle

    @Slot()
    def _on_thread_finished(self) -> None:
        thread = self.sender()
        for handle in list(self._handles):
            if handle.thread is thread:
                self._handles.remove(handle)
                handle.thread.deleteLater()
                break

    @Slot()
    def shutdown(self) -> None:
        # Called from the owning/UI thread. A terminal worker may already have
        # executed deleteLater() while the registry's queued thread.finished
        # cleanup has not yet run. Never invoke a method through an invalid
        # Shiboken wrapper in that window.
        handles = list(self._handles)
        for handle in handles:
            if handle.thread.isRunning():
                if _qt_object_is_valid(handle.worker):
                    QMetaObject.invokeMethod(
                        handle.worker, "deleteLater", Qt.ConnectionType.QueuedConnection)
                handle.thread.quit()
        for handle in handles:
            if QThread.currentThread() is not handle.thread and handle.thread.isRunning():
                handle.thread.wait()
            if _qt_object_is_valid(handle.thread):
                handle.thread.deleteLater()
        self._handles.clear()

class PilotAuditWorker(QObject):
    """Build a Phase-7.2.7 pilot audit snapshot off the GUI thread."""

    progress = Signal(str)
    finished = Signal(object)  # PilotAuditSnapshot
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
            from ppa.pilot_audit import build_pilot_audit
            conn = connect(self._db_path)
            snap = build_pilot_audit(
                conn, library_id=self._library_id,
                progress_cb=self.progress.emit, cancel_cb=self._cancel.is_set)
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                self.finished.emit(snap)
        except PilotAnalysisCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()

class PilotSessionWorker(QObject):
    """Run integrity-checked pilot-session operations off the GUI thread.

    Operations are intentionally narrow: ``start``, ``refresh``, ``checkpoint`` and
    ``close``.  Mutating operations save the external session JSON atomically only
    after the new session object has been completely built and validated.
    """

    progress = Signal(str)
    finished = Signal(object, object)  # PilotSession, current PilotAuditSnapshot | None
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, db_path: Path, operation: str, session_path: Path, *,
                 library_id: int | None = None, directory_prefix: str | None = None,
                 file_ids=None, label: str | None = None) -> None:
        super().__init__()
        self._db_path = db_path
        self._operation = operation
        self._session_path = Path(session_path)
        self._library_id = library_id
        self._directory_prefix = directory_prefix
        self._file_ids = tuple(file_ids) if file_ids is not None else None
        self._label = label
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.pilot import PilotAnalysisCancelled
            from ppa.pilot_audit import build_pilot_audit
            from ppa.pilot_session import (
                checkpoint_pilot_session, close_pilot_session, load_pilot_session,
                save_pilot_session, start_pilot_session,
            )
            conn = connect(self._db_path)
            op = self._operation
            if op == "start":
                if self._library_id is None:
                    raise ValueError("library_id is required to start a pilot")
                if self._session_path.exists():
                    raise ValueError("pilot session file already exists")
                session = start_pilot_session(
                    conn, library_id=self._library_id, directory_prefix=self._directory_prefix,
                    file_ids=self._file_ids, progress_cb=self.progress.emit,
                    cancel_cb=self._cancel.is_set)
                if self._cancel.is_set():
                    self.cancelled.emit(); return
                save_pilot_session(session, self._session_path)
                self.finished.emit(session, session.baseline)
                return

            session = load_pilot_session(self._session_path)
            if op == "refresh":
                self.progress.emit("Pilot Dashboard: refreshing current audit…")
                current = build_pilot_audit(
                    conn, library_id=session.library_id,
                    directory_prefix=session.directory_prefix,
                    file_ids=session.explicit_file_ids,
                    progress_cb=self.progress.emit, cancel_cb=self._cancel.is_set)
                if (current.library_root, current.directory_prefix, current.explicit_file_ids) != (
                        session.library_root, session.directory_prefix, session.explicit_file_ids):
                    raise ValueError("pilot scope no longer resolves to the original library/root")
                if self._cancel.is_set():
                    self.cancelled.emit(); return
                self.finished.emit(session, current)
                return
            if op == "checkpoint":
                updated = checkpoint_pilot_session(
                    conn, session, label=self._label,
                    progress_cb=self.progress.emit, cancel_cb=self._cancel.is_set)
                if self._cancel.is_set():
                    self.cancelled.emit(); return
                save_pilot_session(updated, self._session_path)
                self.finished.emit(updated, updated.checkpoints[-1].snapshot)
                return
            if op == "close":
                updated = close_pilot_session(
                    conn, session, progress_cb=self.progress.emit,
                    cancel_cb=self._cancel.is_set)
                if self._cancel.is_set():
                    self.cancelled.emit(); return
                save_pilot_session(updated, self._session_path)
                self.finished.emit(updated, updated.final)
                return
            raise ValueError(f"unknown pilot-session operation: {op}")
        except PilotAnalysisCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()

class ReviewProgressExportWorker(QObject):
    """Export a shareable Phase-7 progress bundle without blocking Qt."""

    finished = Signal(object)  # Path
    failed = Signal(str)

    def __init__(self, config, session, current, destination: Path) -> None:
        super().__init__()
        self._config = config
        self._session = session
        self._current = current
        self._destination = Path(destination)

    @Slot()
    def run(self) -> None:
        try:
            from ppa.review_report import export_review_progress
            path = export_review_progress(
                self._config, self._session, self._current, self._destination)
            self.finished.emit(path)
        except Exception as exc:
            self.failed.emit(str(exc))


class TimelineWorker(QObject):
    """Build the Phase-8 timeline projection off the GUI thread."""

    progress = Signal(str)
    finished = Signal(object)  # TimelineView
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, db_path: Path, library_id: int, *, directory_prefix: str | None = None, file_ids=None) -> None:
        super().__init__()
        self._db_path = db_path
        self._library_id = library_id
        self._directory_prefix = directory_prefix
        self._file_ids = tuple(file_ids) if file_ids is not None else None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.pilot import PilotAnalysisCancelled
            from ppa.timeline import build_timeline
            conn = connect(self._db_path)
            view = build_timeline(
                conn, library_id=self._library_id,
                directory_prefix=self._directory_prefix, file_ids=self._file_ids,
                progress_cb=self.progress.emit, cancel_cb=self._cancel.is_set)
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                self.finished.emit(view)
        except PilotAnalysisCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()

class OrganizationActivityWorker(QObject):
    """Build Phase-9.11 organisation activity off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int, limit: int = 300) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id; self._limit=limit
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_activity import build_organization_activity
            conn=connect(self._db_path)
            self.finished.emit(build_organization_activity(conn,library_id=self._library_id,limit=self._limit))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationUndoWorker(QObject):
    """Perform one fail-closed audited membership undo off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int, history_id: int) -> None:
        super().__init__(); self._db_path=db_path; self._library_id=library_id; self._history_id=history_id
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_activity import undo_organization_membership
            conn=connect(self._db_path)
            self.finished.emit(undo_organization_membership(conn,library_id=self._library_id,history_id=self._history_id))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class OrganizationReportWorker(QObject):
    """Export one sanitized Phase-9.12 organisation report ZIP off-thread."""
    finished = Signal(object)
    failed = Signal(str)
    def __init__(self, db_path: Path, library_id: int, output_path: Path) -> None:
        super().__init__(); self._db_path=Path(db_path); self._library_id=library_id; self._output_path=Path(output_path)
    @Slot()
    def run(self) -> None:
        conn=None
        try:
            from ppa.organization_report import export_organization_report_zip
            conn=connect(self._db_path)
            self.finished.emit(export_organization_report_zip(conn,library_id=self._library_id,output_path=self._output_path))
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            if conn is not None: conn.close()


class DuplicateLineageReviewWorker(QObject):
    """Build the Phase-10 duplicate-identity projection off the GUI thread."""
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: Path, library_id: int) -> None:
        super().__init__()
        self._db_path = Path(db_path)
        self._library_id = library_id

    @Slot()
    def run(self) -> None:
        conn = None
        try:
            from ppa.duplicate_lineage import build_duplicate_identity
            from ppa.identity_health import build_identity_health
            conn = connect(self._db_path)
            self.finished.emit((
                build_duplicate_identity(conn, library_id=self._library_id),
                build_identity_health(conn, library_id=self._library_id),
            ))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()
