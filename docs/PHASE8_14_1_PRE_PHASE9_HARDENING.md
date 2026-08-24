# Phase 8.14.1 — Pre-Phase-9 Hardening

Date: 2026-08-24

This slice responds to the independent pre-Phase-9 adversarial review of
Phase 8.14. It changes no chronology, Event, reconstruction or source-photo
authority. It hardens model semantics, projection performance, Qt thread
boundaries and SQLite lifecycle management before Phase 9 begins.

## Closed findings

### Needs Attention dead zone

`EventHealth` now owns an explicit `needs_attention` property. Under the
current completion contract it is exactly `not curation_complete`, so Events
with hidden/out-of-scope members are actionable even when they already have a
story and no visible tentative/unplaced/stale members. UI filtering consumes
that model property rather than reconstructing a partial predicate.

### Family History health performance / GUI freeze

`EventHomeWorker` now returns four immutable projections:

1. `TimelineView`
2. `EventHomeView`
3. `EventSearchIndex`
4. `EventHealthView`

The dialog performs no collection-wide health calculation in its constructor.
The health builder indexes the Timeline once and fetches Event IDs, members,
context and presentation state in four library-scoped SELECTs. A regression
with 80 Events asserts a bounded SELECT count, preventing recurrence of the
old N+1 / O(Events × Timeline) composition.

### Qt worker boundary

Worker-to-GUI application callbacks are decorated Qt slots and are explicitly
connected with queued delivery on the major Phase 7/8 worker paths. The
cross-thread batch-sampling lambda was removed.

`WorkerRegistry` is now a `QObject`. Terminal worker signals schedule worker
deletion and request `QThread.quit()`. Registry cleanup is driven by the
thread's `finished` signal through a decorated receiver on the registry's
owning thread; no worker-thread lambda calls `wait()`.

A PySide6/offscreen regression asserts that a worker emits from a non-GUI
thread while its GUI-side receiver executes on `QApplication.thread()`.

### SQLite lifecycle

`ScanWorker`, `VerifyWorker` and `MetadataWorker` now close their worker-owned
connection in `finally`, including exception paths. `MainWindow.closeEvent()`
shuts down workers and explicitly closes the long-lived GUI catalogue
connection.

The conditional PySide6 smoke suite includes a failure-path regression for all
three legacy workers.

### Phase-8 Qt smoke coverage

The conditional offscreen suite now includes:

- Family History Needs Attention with hidden members;
- Curation Complete filtering;
- Event search interaction;
- Timeline dialog construction;
- Event Story dialog construction;
- worker→GUI thread-affinity assertion;
- legacy worker connection closure on failure.

The current review container does not include PySide6, so this module remains a
single skip here. It is intended to run as a required gate in the real Windows
PySide6 environment.

## Test warning hygiene

Python 3.13 still reports ResourceWarnings from numerous older tests that create
catalogue connections without explicitly closing them. Production worker
failure-path leaks are fixed in this slice. The remaining warning cleanup is a
test-suite hygiene task; warnings are not suppressed or masked.

## Authority / source safety

This hardening slice performs no new date inference and introduces no source
photo mutation path. Phase-8 Event/story/presentation/navigation layers remain
downstream of Timeline chronology authority.
