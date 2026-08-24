from __future__ import annotations
from pathlib import Path
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (QAbstractItemView,QDialog,QHBoxLayout,QLabel,QMessageBox,
                               QPushButton,QTableWidget,QTableWidgetItem,QVBoxLayout)
from ppa.ui.workers import WorkerRegistry, OrganizationActivityWorker, OrganizationUndoWorker

class OrganizationActivityDialog(QDialog):
    def __init__(self, db_path: Path, view, parent=None) -> None:
        super().__init__(parent); self._db_path=Path(db_path); self._view=view; self._registry=WorkerRegistry()
        self.setWindowTitle('Organisation Activity'); self.resize(980,560)
        root=QVBoxLayout(self)
        note=QLabel('Append-only Album/Tag curation history. Automatic Undo is limited to provably current membership changes; ambiguous history remains review-only.')
        note.setWordWrap(True); root.addWidget(note)
        self._table=QTableWidget(0,5); self._table.setHorizontalHeaderLabels(['When','Kind','Object','Change','Undo'])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection); self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._selection_changed); root.addWidget(self._table,1)
        buttons=QHBoxLayout(); self._undo=QPushButton('Undo membership change…'); self._undo.clicked.connect(self._undo_selected); buttons.addWidget(self._undo)
        self._refresh=QPushButton('Refresh'); self._refresh.clicked.connect(self._refresh_view); buttons.addWidget(self._refresh); buttons.addStretch(1)
        close=QPushButton('Close'); close.clicked.connect(self.close); buttons.addWidget(close); root.addLayout(buttons)
        self._status=QLabel(); root.addWidget(self._status); self._populate()

    def _populate(self):
        self._table.setRowCount(0)
        for e in self._view.entries:
            r=self._table.rowCount(); self._table.insertRow(r)
            vals=[e.created_at,e.object_kind.title(),e.object_name,e.summary,'Yes' if e.undoable else (e.undo_reason or 'No')]
            for c,v in enumerate(vals):
                it=QTableWidgetItem(v); it.setData(Qt.ItemDataRole.UserRole,e if c==0 else None); self._table.setItem(r,c,it)
        self._table.resizeColumnsToContents(); self._table.horizontalHeader().setStretchLastSection(True)
        self._status.setText(f'{len(self._view.entries)} recent organisation change(s).')
        if self._table.rowCount(): self._table.selectRow(0)
        self._selection_changed()

    def _selected(self):
        r=self._table.currentRow(); return None if r<0 else self._table.item(r,0).data(Qt.ItemDataRole.UserRole)

    def _selection_changed(self):
        e=self._selected(); self._undo.setEnabled(bool(e and e.undoable))

    def _undo_selected(self):
        e=self._selected()
        if e is None or not e.undoable: return
        ans=QMessageBox.question(self,'Undo organisation change',f'{e.summary}\n\nUndo this membership change?\n\nThe current state will be revalidated before commit.',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if ans!=QMessageBox.StandardButton.Yes: return
        self._undo.setEnabled(False); self._status.setText('Revalidating and undoing membership change…')
        w=OrganizationUndoWorker(self._db_path,self._view.library_id,e.id); w.finished.connect(self._undone,Qt.ConnectionType.QueuedConnection); w.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection); self._registry.start(w)

    @Slot(object)
    def _undone(self,_entry):
        self._status.setText('Undo committed and audited. Refreshing…'); self._refresh_view()

    def _refresh_view(self):
        self._refresh.setEnabled(False); w=OrganizationActivityWorker(self._db_path,self._view.library_id); w.finished.connect(self._refreshed,Qt.ConnectionType.QueuedConnection); w.failed.connect(self._failed,Qt.ConnectionType.QueuedConnection); self._registry.start(w)

    @Slot(object)
    def _refreshed(self,view):
        self._view=view; self._refresh.setEnabled(True); self._populate()

    @Slot(str)
    def _failed(self,message):
        self._refresh.setEnabled(True); self._selection_changed(); self._status.setText('Operation failed or the history entry became stale.'); QMessageBox.warning(self,'Organisation Activity',message)

    def closeEvent(self,event):  # noqa: N802
        self._registry.shutdown(); super().closeEvent(event)
