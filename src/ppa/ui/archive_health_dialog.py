"""Phase 12.1 — Backup & Archive Health desktop summary."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from ppa.ui.workers import ArchiveHealthBrowseWorker, WorkerRegistry


class ArchiveHealthDialog(QDialog):
    def __init__(self, conn, db_path: Path, health, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._db_path = Path(db_path)
        self._health = health
        self._cache_dir = Path(cache_dir or Path.home() / '.cache' / 'personal-photo-archive' / 'archive-health')
        self._registry = WorkerRegistry()
        self._browse_worker = None
        self._browser = None

        self.setWindowTitle('Backup & Archive Health')
        self.resize(860, 720)
        root = QVBoxLayout(self)

        title = QLabel('Backup & Archive Health')
        title.setObjectName('SectionHeader')
        root.addWidget(title)

        note = QLabel(
            'Read-only catalogue health. Normal scans now capture filesystem device/object identity where the platform '
            'provides it, so hard-linked paths can be distinguished from distinct file objects. Distinct device IDs are '
            'stronger filesystem evidence, but still are not proof of independent physical backup hardware or failure domains.'
        )
        note.setWordWrap(True)
        root.addWidget(note)

        totals = QLabel(
            f'{health.total_photos} logical photos · {health.total_files} catalogued Files · '
            f'{health.present_files} present · {health.missing_files} missing'
        )
        totals.setObjectName('FieldKey')
        root.addWidget(totals)

        grid = QGridLayout()
        root.addLayout(grid)
        rows = [
            ('Needs attention (unique logical Photos)', health.attention_count, 'attention'),
            ('No present catalogued File', health.no_present_count, 'no_present'),
            ('One present catalogued File', health.single_present_count, 'single_present'),
            ('Multiple exact present Files', health.multiple_exact_present_count, 'multiple_exact'),
            ('Some catalogued copies missing', health.missing_copy_photo_count, 'missing_copies'),
            ('Present-file health warnings', health.unhealthy_present_count, 'unhealthy'),
            ('Present Files without current SHA-256', health.unknown_hash_count, 'unknown_hash'),
            ('Current content divergence', health.divergent_count, 'divergent'),
            ('Exact sets with unknown storage identity', health.exact_storage_unknown_count, 'storage_unknown'),
            ('Exact sets with hard-link path inflation', health.hardlink_overstated_count, 'hardlinks'),
            ('Exact sets spanning distinct filesystem objects', health.distinct_file_object_count, 'distinct_objects'),
            ('Exact sets spanning distinct filesystem device IDs', health.distinct_device_count, 'distinct_devices'),
        ]
        for r, (label, count, category) in enumerate(rows):
            grid.addWidget(QLabel(label), r, 0)
            value = QLabel(str(count))
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(value, r, 1)
            btn = QPushButton('Browse…')
            btn.setEnabled(count > 0)
            btn.clicked.connect(lambda _=False, c=category: self._browse(c))
            grid.addWidget(btn, r, 2)

        self._status = QLabel(
            'Review catalogue coverage and filesystem-object evidence before treating path counts as backup assurance.'
            if health.attention_count else
            'No current catalogue attention indicators. Independent physical backup hardware is still not proven.'
        )
        self._status.setObjectName('FieldKey')
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        root.addStretch(1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close = QPushButton('Close')
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        root.addLayout(bottom)

    def _browse(self, category: str) -> None:
        self._status.setText('Building read-only Archive Health photo view…')
        worker = ArchiveHealthBrowseWorker(self._db_path, self._health, category)
        self._browse_worker = worker
        worker.finished.connect(self._browse_ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._browse_failed, Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _browse_ready(self, view) -> None:
        self._status.setText(f'{view.total_members} logical photo(s) in this Archive Health category.')
        from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog
        dialog = OrganizationBrowseDialog(
            self._conn, 'archive_health', view.object_id, self,
            cache_dir=self._cache_dir / 'browse', view=view,
        )
        dialog.show()
        self._browser = dialog

    @Slot(str)
    def _browse_failed(self, message: str) -> None:
        self._status.setText('Archive Health browse failed.')
        QMessageBox.warning(self, 'Backup & Archive Health', message)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._registry.shutdown()
        super().closeEvent(event)
