"""Phase 9.9 — review-first assisted organisation suggestions UI."""
from __future__ import annotations
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QInputDialog,
)

from ppa.ui.workers import (
    OrganizationSuggestionApplyWorker, OrganizationSuggestionBrowseWorker,
    OrganizationSuggestionDismissWorker, OrganizationSuggestionReviewsWorker,
    OrganizationSuggestionRestoreWorker, OrganizationSuggestionsWorker, WorkerRegistry,
)


class OrganizationSuggestionsDialog(QDialog):
    def __init__(self, conn, db_path: Path, view, parent=None, *, cache_dir: Path | None = None) -> None:
        super().__init__(parent)
        self._conn=conn; self._db_path=Path(db_path); self._view=view
        self._cache_dir=Path(cache_dir or Path.home()/'.cache'/'personal-photo-archive'/'organization-suggestions')
        self._registry=WorkerRegistry(); self._browser=None
        self.setWindowTitle('Assisted Organisation'); self.resize(920,560)
        root=QVBoxLayout(self)
        title=QLabel('Assisted Organisation'); title.setObjectName('SectionHeader'); root.addWidget(title)
        note=QLabel(
            'Review-only suggestions derived from explicit Event/Album/Tag membership. '
            'PPA never invents a Tag or applies a suggestion without your approval.')
        note.setWordWrap(True); root.addWidget(note)
        self._table=QTableWidget(0,6); self._table.setHorizontalHeaderLabels(
            ['Source','Group','Suggested Tag','Support','Review photos','Reason'])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._selection_changed)
        root.addWidget(self._table,1)
        buttons=QHBoxLayout()
        self._review=QPushButton('Review photos…'); self._review.clicked.connect(self._review_selected); buttons.addWidget(self._review)
        self._apply=QPushButton('Apply suggested Tag…'); self._apply.clicked.connect(self._apply_selected); buttons.addWidget(self._apply)
        self._dismiss=QPushButton('Dismiss…'); self._dismiss.clicked.connect(self._dismiss_selected); buttons.addWidget(self._dismiss)
        self._history=QPushButton('Reviewed…'); self._history.clicked.connect(self._show_reviewed); buttons.addWidget(self._history)
        self._refresh=QPushButton('Refresh'); self._refresh.clicked.connect(self._refresh_view); buttons.addWidget(self._refresh)
        buttons.addStretch(1); close=QPushButton('Close'); close.clicked.connect(self.close); buttons.addWidget(close); root.addLayout(buttons)
        self._status=QLabel(); root.addWidget(self._status)
        self._populate()

    def _populate(self) -> None:
        self._table.setRowCount(0)
        for s in self._view.suggestions:
            r=self._table.rowCount(); self._table.insertRow(r)
            vals=[s.group_kind.title(),s.group_name,s.tag_name,
                  f'{s.tagged_count}/{s.peer_count} ({s.coverage:.0%})',str(s.target_count),s.rationale]
            for c,v in enumerate(vals):
                item=QTableWidgetItem(v); item.setData(Qt.ItemDataRole.UserRole,s if c==0 else None); self._table.setItem(r,c,item)
        self._table.resizeColumnsToContents(); self._table.horizontalHeader().setStretchLastSection(True)
        self._status.setText(f'{len(self._view.suggestions)} suggestion(s) · {self._view.candidate_photo_count} unique review candidate photo(s).')
        if self._table.rowCount(): self._table.selectRow(0)
        self._selection_changed()

    def _selected(self):
        row=self._table.currentRow()
        return None if row < 0 else self._table.item(row,0).data(Qt.ItemDataRole.UserRole)

    def _selection_changed(self) -> None:
        enabled=self._selected() is not None
        self._review.setEnabled(enabled); self._apply.setEnabled(enabled); self._dismiss.setEnabled(enabled)

    def _review_selected(self) -> None:
        s=self._selected()
        if s is None: return
        self._status.setText('Building logical-photo review view…')
        worker=OrganizationSuggestionBrowseWorker(self._db_path,s)
        worker.finished.connect(self._review_ready,Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _review_ready(self, browse) -> None:
        from ppa.ui.organization_browse_dialog import OrganizationBrowseDialog
        dialog=OrganizationBrowseDialog(self._conn,'organization_suggestion',browse.object_id,self,
                                         cache_dir=self._cache_dir/'browse',view=browse)
        dialog.show(); self._browser=dialog; self._status.setText(f'Reviewing {browse.total_members} candidate photo(s).')

    def _apply_selected(self) -> None:
        s=self._selected()
        if s is None: return
        answer=QMessageBox.question(
            self,'Apply suggested Tag',
            f"Apply Tag '{s.tag_name}' to {s.target_count} reviewed logical photo(s) from {s.group_kind} '{s.group_name}'?\n\n"
            'The suggestion will be revalidated before commit. This changes Tag membership only.',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes: return
        self._apply.setEnabled(False); self._status.setText('Revalidating and applying Tag…')
        worker=OrganizationSuggestionApplyWorker(self._db_path,s)
        worker.finished.connect(self._applied,Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _applied(self, _tag) -> None:
        self._status.setText('Suggestion accepted and applied through audited Tag membership. Refreshing…'); self._refresh_view()

    def _dismiss_selected(self) -> None:
        s=self._selected()
        if s is None: return
        note, ok = QInputDialog.getMultiLineText(
            self, 'Dismiss suggestion',
            'Optional note — why should this exact unchanged suggestion stay hidden?', '')
        if not ok: return
        self._dismiss.setEnabled(False); self._status.setText('Revalidating and dismissing suggestion…')
        worker=OrganizationSuggestionDismissWorker(self._db_path,s,note)
        worker.finished.connect(self._dismissed,Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _dismissed(self, _review) -> None:
        self._status.setText('Suggestion dismissed for this exact fingerprint. Refreshing…')
        self._refresh_view()

    def _show_reviewed(self) -> None:
        self._history.setEnabled(False); self._status.setText('Loading suggestion review history…')
        worker=OrganizationSuggestionReviewsWorker(self._db_path,self._view.library_id)
        worker.finished.connect(self._reviewed_ready,Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _reviewed_ready(self, reviews) -> None:
        self._history.setEnabled(True)
        dialog=SuggestionReviewsDialog(self._db_path,self._view.library_id,reviews,self)
        dialog.restored.connect(self._refresh_view)
        dialog.show(); self._review_history_dialog=dialog
        self._status.setText(f'{len(reviews)} reviewed suggestion fingerprint(s).')

    def _refresh_view(self) -> None:
        self._refresh.setEnabled(False)
        worker=OrganizationSuggestionsWorker(self._db_path,self._view.library_id)
        worker.finished.connect(self._refreshed,Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _refreshed(self, view) -> None:
        self._view=view; self._refresh.setEnabled(True); self._populate()

    @Slot(str)
    def _failed(self, message: str) -> None:
        self._apply.setEnabled(self._selected() is not None); self._dismiss.setEnabled(self._selected() is not None); self._refresh.setEnabled(True); self._history.setEnabled(True)
        self._status.setText('Operation failed or suggestion became stale.')
        QMessageBox.warning(self,'Assisted Organisation',message)

    def closeEvent(self,event) -> None:  # noqa: N802
        self._registry.shutdown(); super().closeEvent(event)


from PySide6.QtCore import Signal

class SuggestionReviewsDialog(QDialog):
    restored = Signal()
    def __init__(self, db_path: Path, library_id: int, reviews, parent=None) -> None:
        super().__init__(parent)
        self._db_path=Path(db_path); self._library_id=library_id; self._reviews=tuple(reviews); self._registry=WorkerRegistry()
        self.setWindowTitle('Reviewed Organisation Suggestions'); self.resize(760,420)
        root=QVBoxLayout(self)
        note=QLabel('Review state belongs to an exact suggestion fingerprint. A changed peer pattern produces a new fingerprint and can surface again.')
        note.setWordWrap(True); root.addWidget(note)
        self._table=QTableWidget(0,4); self._table.setHorizontalHeaderLabels(['Status','Reviewed','Fingerprint','Note'])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection); self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self._table,1)
        for review in self._reviews:
            r=self._table.rowCount(); self._table.insertRow(r)
            vals=[review.status.title(),review.reviewed_at,review.suggestion_id[:16]+'…',review.note or '']
            for c,v in enumerate(vals):
                item=QTableWidgetItem(v); item.setData(Qt.ItemDataRole.UserRole,review if c==0 else None); self._table.setItem(r,c,item)
        self._table.resizeColumnsToContents(); self._table.horizontalHeader().setStretchLastSection(True)
        buttons=QHBoxLayout(); self._restore=QPushButton('Restore dismissed'); self._restore.clicked.connect(self._restore_selected); buttons.addWidget(self._restore); buttons.addStretch(1); close=QPushButton('Close'); close.clicked.connect(self.close); buttons.addWidget(close); root.addLayout(buttons)
        self._table.itemSelectionChanged.connect(self._selection_changed)
        if self._table.rowCount(): self._table.selectRow(0)
        self._selection_changed()

    def _selected(self):
        row=self._table.currentRow(); return None if row < 0 else self._table.item(row,0).data(Qt.ItemDataRole.UserRole)

    def _selection_changed(self) -> None:
        review=self._selected(); self._restore.setEnabled(review is not None and review.status=='dismissed')

    def _restore_selected(self) -> None:
        review=self._selected()
        if review is None or review.status != 'dismissed': return
        self._restore.setEnabled(False)
        worker=OrganizationSuggestionRestoreWorker(self._db_path,self._library_id,review.suggestion_id)
        worker.finished.connect(self._restored,Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection)
        self._registry.start(worker)

    @Slot(object)
    def _restored(self, ok) -> None:
        if ok:
            self.restored.emit(); self.accept()
        else:
            QMessageBox.information(self,'Reviewed Suggestions','That dismissal is no longer active.')
            self.accept()

    @Slot(str)
    def _failed(self, message: str) -> None:
        self._restore.setEnabled(True); QMessageBox.warning(self,'Reviewed Suggestions',message)

    def closeEvent(self,event) -> None:  # noqa: N802
        self._registry.shutdown(); super().closeEvent(event)
