"""Phase 9.8 — Organisation Health desktop summary."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from ppa.ui.workers import OrganizationGapWorker, WorkerRegistry


class OrganizationHealthDialog(QDialog):
    def __init__(self, conn, db_path: Path, health, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._conn = conn; self._db_path = Path(db_path); self._health = health
        self._cache_dir = Path(cache_dir or Path.home()/'.cache'/'personal-photo-archive'/'organization-health')
        self._registry = WorkerRegistry(); self._gap_worker = None; self._browser = None
        self.setWindowTitle('Organisation Health'); self.resize(700, 500)
        root = QVBoxLayout(self)
        title = QLabel('Organisation Health'); title.setObjectName('SectionHeader'); root.addWidget(title)
        note = QLabel('Read-only curation indicators. These do not alter Albums, Tags, chronology, evidence, or source photos.')
        note.setWordWrap(True); root.addWidget(note)

        grid = QGridLayout(); root.addLayout(grid)
        rows = [
            ('Logical photos', health.total_photos, None),
            ('Unorganised (no Album and no Tag)', health.unorganized_count, 'unorganized'),
            ('No Album', health.no_album_count, 'no_album'),
            ('No Tags', health.no_tag_count, 'no_tag'),
            ('Empty Albums', len(health.empty_album_ids), None),
            ('Unused Tags', len(health.unused_tag_ids), None),
            ('Albums with missing-only members', len(health.albums_with_missing_only_members), None),
            ('Tags with missing-only members', len(health.tags_with_missing_only_members), None),
            ('Broken saved discovery views', len(health.broken_saved_view_ids), None),
        ]
        for r, (label, count, gap) in enumerate(rows):
            grid.addWidget(QLabel(label), r, 0)
            value = QLabel(str(count)); value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(value, r, 1)
            if gap:
                btn = QPushButton('Browse…'); btn.setEnabled(count > 0)
                btn.clicked.connect(lambda _=False, g=gap: self._browse_gap(g))
                grid.addWidget(btn, r, 2)
        status = 'Needs curation attention' if health.needs_attention else 'No current organisation-health indicators'
        self._status = QLabel(status); self._status.setObjectName('FieldKey'); root.addWidget(self._status)
        root.addStretch(1)
        bottom = QHBoxLayout(); bottom.addStretch(1)
        close = QPushButton('Close'); close.clicked.connect(self.close); bottom.addWidget(close); root.addLayout(bottom)

    def _browse_gap(self, gap: str) -> None:
        self._status.setText('Building logical-photo gap view…')
        worker = OrganizationGapWorker(self._db_path, self._health, gap)
        self._gap_worker = worker
        worker.finished.connect(self._gap_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._gap_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _gap_ready(self, view) -> None:
        self._status.setText(f'{view.total_members} logical photos in this curation gap.')
        from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog
        dialog = OrganizationBrowseDialog(self._conn, 'organization_gap', view.object_id, self,
                                           cache_dir=self._cache_dir/'browse', view=view)
        dialog.show(); self._browser = dialog

    @Slot(str)
    def _gap_failed(self, message: str) -> None:
        self._status.setText('Gap view failed.')
        QMessageBox.warning(self, 'Organisation Health', message)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._registry.shutdown(); super().closeEvent(event)
