"""Phase 10.1 — duplicate identity and Photo-lineage review UI.

The UI keeps three distinct questions separate:

* Exact Copies: current physical Files with the same logical Photo and SHA-256.
* Identity Divergence: one logical Photo whose current Files have different hashes.
* Photo Lineage: explicit human-confirmed relationships between distinct Photos.

Nothing in this module merges, splits, deletes, rewrites or infers source photos.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSplitter, QTabWidget, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ppa.duplicate_lineage import (RELATION_TYPES, add_lineage, list_lineage, remove_lineage,
                                  validate_exact_copy_pair)
from ppa.divergence_investigation import investigate_identity_divergence
from ppa.competing_identity import investigate_competing_identity
from ppa.identity_resolution import (plan_identity_split, execute_identity_split,
                                     list_identity_resolutions, review_identity_resolution,
                                     plan_identity_recovery, execute_identity_recovery)
from ppa.identity_health import build_identity_health
from ppa.identity_merge import plan_identity_merge, execute_identity_merge

_FILE_ID_ROLE = Qt.ItemDataRole.UserRole
_PHOTO_ID_ROLE = Qt.ItemDataRole.UserRole + 1
_LINEAGE_ID_ROLE = Qt.ItemDataRole.UserRole + 2
_SHA_ROLE = Qt.ItemDataRole.UserRole + 3


def _short(value: str | None, n: int = 12) -> str:
    if not value:
        return "unknown"
    return value if len(value) <= n else value[:n] + "…"


def _photo_choices(conn, library_id: int) -> list[tuple[str, str]]:
    """Return deterministic logical-Photo choices represented in this Library."""
    rows = conn.execute(
        """
        SELECT f.photo_id,
               COALESCE(MIN(CASE WHEN f.presence_status='present' THEN f.filename END),
                        MIN(f.filename)) AS label
          FROM files f
         WHERE f.library_id=?
         GROUP BY f.photo_id
         ORDER BY label COLLATE NOCASE, f.photo_id
        """, (library_id,),
    ).fetchall()
    return [(r["photo_id"], r["label"] or _short(r["photo_id"])) for r in rows]


def _library_photo_ids(conn, library_id: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT photo_id FROM files WHERE library_id=?", (library_id,))}


class SideBySidePreviewDialog(QDialog):
    """Read-only side-by-side comparison of two physical File records."""

    def __init__(self, conn, left_file_id: str, right_file_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare Exact Copies")
        self.resize(1180, 700)
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        root.addWidget(self._panel(conn, left_file_id), 1)
        root.addWidget(self._panel(conn, right_file_id), 1)

    def _panel(self, conn, file_id: str) -> QWidget:
        row = conn.execute(
            "SELECT filename,path,presence_status,health_status,sha256 FROM files WHERE id=?",
            (file_id,),
        ).fetchone()
        box = QWidget(self)
        lay = QVBoxLayout(box)
        image = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        image.setMinimumSize(280, 260)
        image.setStyleSheet("background:#111; border:1px solid #444;")
        if row is None:
            title = "Unknown file"
            image.setText("File record unavailable")
        else:
            title = row["filename"]
            path = Path(row["path"])
            if row["presence_status"] != "present" or not path.is_file():
                image.setText("Physical copy is unavailable")
            else:
                reader = QImageReader(str(path))
                reader.setAutoTransform(True)
                img = reader.read()
                if img.isNull():
                    image.setText("Preview unavailable")
                else:
                    pix = QPixmap.fromImage(img)
                    image.setPixmap(pix.scaled(540, 570, Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))
            meta = QLabel(
                f"{row['presence_status']} · {row['health_status']}\nSHA-256 {_short(row['sha256'], 18)}",
                alignment=Qt.AlignmentFlag.AlignCenter,
            )
            meta.setWordWrap(True)
            lay.addWidget(QLabel(f"<b>{title}</b>", alignment=Qt.AlignmentFlag.AlignCenter))
            lay.addWidget(image, 1)
            lay.addWidget(meta)
            return box
        lay.addWidget(QLabel(f"<b>{title}</b>", alignment=Qt.AlignmentFlag.AlignCenter))
        lay.addWidget(image, 1)
        return box


class DivergenceInvestigationDialog(QDialog):
    """Read-only FileRevision evidence for one logical-Photo divergence."""
    def __init__(self, investigation, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Identity Divergence Investigation")
        self.resize(1100, 720)
        root = QVBoxLayout(self)
        verdict = QLabel(
            f"<b>{investigation.classification.replace('_', ' ').upper()}</b><br>"
            f"Logical Photo {_short(investigation.photo_id)} — {investigation.rationale}"
        )
        verdict.setWordWrap(True)
        root.addWidget(verdict)
        note = QLabel(
            "This is observation-only forensic context. It does not split/merge Photo identity, "
            "create lineage, select an original, delete a copy, or modify source files."
        )
        note.setWordWrap(True); root.addWidget(note)
        tree = QTreeWidget(self)
        tree.setHeaderLabels(["File / revision", "Observed", "SHA-256", "State / filesystem mtime"])
        tree.setAlternatingRowColors(True)
        for f in investigation.files:
            top = QTreeWidgetItem([
                f.filename,
                f"first {f.first_seen_at} · last {f.last_seen_at}",
                _short(f.current_sha256, 18),
                f"{f.presence_status} · {f.health_status}" + (" · MODIFIED IN PLACE" if f.modified_in_place else ""),
            ])
            tree.addTopLevelItem(top)
            if not f.revisions:
                top.addChild(QTreeWidgetItem(["No immutable revision history", "", "", "INSUFFICIENT EVIDENCE"]))
            for rev in f.revisions:
                state = "CURRENT" if rev.is_current else (f"superseded {rev.superseded_at}" if rev.superseded_at else "historical")
                child = QTreeWidgetItem([
                    f"Revision {_short(rev.revision_id)}", rev.first_observed_at,
                    _short(rev.sha256, 18), f"{state} · mtime {rev.fs_mtime or 'unknown'}",
                ])
                top.addChild(child)
            top.setExpanded(True)
        tree.header().setStretchLastSection(True)
        root.addWidget(tree, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject); buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        root.addWidget(buttons)
        self.evidence_tree = tree


class CompetingIdentityInvestigationDialog(QDialog):
    """Forensic evidence plus explicit controlled merge for eligible cases."""
    def __init__(self, investigation, parent=None, conn=None) -> None:
        super().__init__(parent)
        self._investigation = investigation
        self._conn = conn
        self.setWindowTitle("Competing Identity Investigation")
        self.resize(1120, 740)
        root = QVBoxLayout(self)
        merge = investigation.merge_consideration
        status = "MERGE MAY BE CONSIDERED" if merge.eligible else "REVIEW ONLY"
        head = QLabel(
            f"<b>{investigation.classification.replace('_', ' ').upper()}</b><br>"
            f"Shared SHA-256 {_short(investigation.sha256, 24)} across {len(investigation.photos)} logical Photos.<br>"
            f"{investigation.rationale}"
        )
        head.setWordWrap(True); root.addWidget(head)
        gate = QLabel(f"<b>{status}</b> — {merge.rationale}")
        gate.setWordWrap(True); root.addWidget(gate)
        if merge.blockers:
            blockers = QLabel("Merge blockers:\n• " + "\n• ".join(merge.blockers))
            blockers.setWordWrap(True); root.addWidget(blockers)
        note = QLabel(
            "This investigation is read-only. Eligibility means only that a future controlled merge workflow could be reviewed; "
            "it does not select a winning identity, merge Photos, alter organisation, create lineage, or touch source files."
        )
        note.setWordWrap(True); root.addWidget(note)
        tree = QTreeWidget(self)
        tree.setHeaderLabels(["Photo / File / revision", "Observed", "SHA-256", "Library / state"])
        tree.setAlternatingRowColors(True)
        for photo in investigation.photos:
            top = QTreeWidgetItem([f"Logical Photo {_short(photo.photo_id)}", f"created {photo.created_at}", "", f"{len(photo.files)} File(s)"])
            tree.addTopLevelItem(top)
            for f in photo.files:
                file_item = QTreeWidgetItem([
                    f.filename, f"first {f.first_seen_at} · last {f.last_seen_at}", _short(f.current_sha256,18),
                    f"Library {f.library_id} · {f.presence_status} · {f.health_status}" + (" · CHANGED TO SHARED BYTES" if f.changed_to_shared_bytes else ""),
                ])
                top.addChild(file_item)
                if not f.revisions:
                    file_item.addChild(QTreeWidgetItem(["No immutable revision history", "", "", "INSUFFICIENT HISTORY"]))
                for rev in f.revisions:
                    state = "CURRENT" if rev.is_current else (f"superseded {rev.superseded_at}" if rev.superseded_at else "historical")
                    file_item.addChild(QTreeWidgetItem([f"Revision {_short(rev.revision_id)}", rev.first_observed_at, _short(rev.sha256,18), state]))
                file_item.setExpanded(True)
            top.setExpanded(True)
        tree.header().setStretchLastSection(True); root.addWidget(tree, 1); self.evidence_tree = tree
        self.merge_controls = QWidget(self)
        merge_row = QHBoxLayout(self.merge_controls); merge_row.setContentsMargins(0,0,0,0)
        self.merge_label = QLabel("Controlled merge — explicitly choose which logical Photo identity survives:")
        self.survivor_combo = QComboBox(self.merge_controls)
        for photo in investigation.photos:
            self.survivor_combo.addItem(f"Photo {_short(photo.photo_id)}", photo.photo_id)
        self.merge_btn = QPushButton("Merge competing identities…", self.merge_controls)
        self.merge_btn.clicked.connect(self._controlled_merge)
        merge_row.addWidget(self.merge_label); merge_row.addWidget(self.survivor_combo); merge_row.addWidget(self.merge_btn)
        root.addWidget(self.merge_controls)
        self.merge_controls.setVisible(bool(investigation.merge_consideration.eligible and conn is not None))
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept); root.addWidget(buttons)

    def _controlled_merge(self) -> None:
        if self._conn is None or not self._investigation.merge_consideration.eligible:
            return
        survivor = self.survivor_combo.currentData()
        try:
            plan = plan_identity_merge(self._conn, library_id=self._investigation.library_id,
                                       sha256=self._investigation.sha256, survivor_photo_id=survivor)
        except Exception as exc:
            QMessageBox.warning(self, "Controlled Identity Merge", str(exc)); return
        if QMessageBox.question(
            self, "Confirm Controlled Identity Merge",
            f"Keep logical Photo {_short(plan.survivor_photo_id)} and retire logical Photo {_short(plan.retired_photo_id)}?\n\n"
            f"{len(plan.moved_file_ids)} physical File(s) will be reassigned in the catalogue only. "
            "No source file will be moved, deleted, or rewritten. PPA will revalidate the complete evidence state under a write lock before commit."
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            execute_identity_merge(self._conn, plan, note="Human-confirmed from Competing Identity Investigation UI")
        except Exception as exc:
            QMessageBox.warning(self, "Controlled Identity Merge", str(exc)); return
        QMessageBox.information(self, "Identity Merge Complete",
                                "The competing logical identities were merged atomically. Source files were untouched and the merge audit was preserved.")
        parent = self.parent()
        self.accept()
        if isinstance(parent, QDialog):
            parent.accept()


class IdentityResolutionReviewDialog(QDialog):
    """Read-only topology and recovery eligibility for one audited split."""
    def __init__(self, review, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Identity Resolution Review")
        self.resize(1000, 650)
        root = QVBoxLayout(self)
        status = "RECOVERY ELIGIBLE" if review.recovery_eligible else ("RECOMBINED" if review.recovered else "REVIEW ONLY")
        head = QLabel(
            f"<b>{status}</b><br>Split {review.created_at}<br>"
            f"Source Photo {_short(review.source_photo_id)} → split-created Photo {_short(review.new_photo_id)}<br>"
            f"Hash cohort {_short(review.split_sha256, 24)}<br>{review.recovery_reason}"
        )
        head.setWordWrap(True); root.addWidget(head)
        info = QLabel(
            "Before the split, the moved cohort belonged to the source logical Photo. The tree below shows the current "
            "post-split topology. Phase 10.3 did not store a full immutable snapshot of every non-moved File, so PPA "
            "does not fabricate unavailable historical topology."
        )
        info.setWordWrap(True); root.addWidget(info)
        tree = QTreeWidget(); tree.setHeaderLabels(["Topology", "SHA-256", "Library / state"])
        before = QTreeWidgetItem([f"BEFORE: source Photo {_short(review.source_photo_id)}", "", "audited moved cohort + retained source identity"])
        tree.addTopLevelItem(before)
        for fid in review.moved_file_ids:
            before.addChild(QTreeWidgetItem([f"Moved cohort File {_short(fid)}", _short(review.split_sha256,18), "belonged to source before split"]))
        after_source = QTreeWidgetItem([f"NOW: source Photo {_short(review.source_photo_id)}", "", f"{len(review.source_files_now)} File(s)"])
        tree.addTopLevelItem(after_source)
        for f in review.source_files_now:
            after_source.addChild(QTreeWidgetItem([f.filename, _short(f.sha256,18), f"Library {f.library_id} · {f.presence_status}"]))
        after_new = QTreeWidgetItem([f"NOW: split-created Photo {_short(review.new_photo_id)}", "", f"{len(review.new_photo_files_now)} File(s)"])
        tree.addTopLevelItem(after_new)
        for f in review.new_photo_files_now:
            after_new.addChild(QTreeWidgetItem([f.filename, _short(f.sha256,18), f"Library {f.library_id} · {f.presence_status}"]))
        before.setExpanded(True); after_source.setExpanded(True); after_new.setExpanded(True)
        root.addWidget(tree, 1); self.topology_tree = tree
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept); root.addWidget(buttons)


class AddLineageDialog(QDialog):
    def __init__(self, conn, library_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Photo Lineage")
        self._choices = _photo_choices(conn, library_id)
        root = QVBoxLayout(self)
        note = QLabel("Create a human-confirmed parent → derivative relationship. This does not merge Photos or change chronology.")
        note.setWordWrap(True)
        root.addWidget(note)
        form = QFormLayout()
        self.parent_combo = QComboBox()
        self.child_combo = QComboBox()
        for photo_id, label in self._choices:
            text = f"{label}   [{_short(photo_id)}]"
            self.parent_combo.addItem(text, photo_id)
            self.child_combo.addItem(text, photo_id)
        if self.child_combo.count() > 1:
            self.child_combo.setCurrentIndex(1)
        self.relation_combo = QComboBox()
        for relation in RELATION_TYPES:
            self.relation_combo.addItem(relation.replace("_", " ").title(), relation)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Optional human note")
        form.addRow("Parent/original:", self.parent_combo)
        form.addRow("Child/derivative:", self.child_combo)
        form.addRow("Relationship:", self.relation_combo)
        form.addRow("Note:", self.note_edit)
        root.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def values(self) -> tuple[str, str, str, str | None]:
        note = self.note_edit.text().strip() or None
        return (self.parent_combo.currentData(), self.child_combo.currentData(),
                self.relation_combo.currentData(), note)


class DuplicateLineageDialog(QDialog):
    def __init__(self, conn, library_id: int, identity_view, parent=None, identity_health=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._library_id = library_id
        self._identity = identity_view
        self._health = identity_health if identity_health is not None else build_identity_health(conn, library_id=library_id)
        self._child_windows: list[QDialog] = []
        self.setWindowTitle("Duplicates & Lineage")
        self.resize(1050, 700)
        root = QVBoxLayout(self)
        intro = QLabel(
            "Review exact physical copies, logical-identity divergence, and explicit Photo lineage. "
            "This surface never auto-merges, auto-splits, deletes, or infers relationships."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_exact_tab()
        self._build_divergence_tab()
        self._build_lineage_tab()
        self._build_resolution_tab()
        self._build_health_tab()

    def _build_exact_tab(self) -> None:
        page = QWidget(); lay = QVBoxLayout(page)
        self.exact_tree = QTreeWidget()
        self.exact_tree.setHeaderLabels(["Photo / File", "State", "SHA-256"])
        self.exact_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        for group in self._identity.sets:
            top = QTreeWidgetItem([f"Logical Photo {_short(group.photo_id)} — {group.copy_count} exact copies",
                                   f"{group.present_count} present", ""])
            top.setData(0, _PHOTO_ID_ROLE, group.photo_id)
            self.exact_tree.addTopLevelItem(top)
            for copy in group.copies:
                child = QTreeWidgetItem([copy.filename,
                                         f"{copy.presence_status} · {copy.health_status}",
                                         _short(copy.sha256, 18)])
                child.setData(0, _FILE_ID_ROLE, copy.file_id)
                child.setData(0, _PHOTO_ID_ROLE, copy.photo_id)
                top.addChild(child)
            top.setExpanded(True)
        lay.addWidget(self.exact_tree, 1)
        self.compare_btn = QPushButton("Compare selected copies…")
        self.compare_btn.clicked.connect(self._compare_selected)
        lay.addWidget(self.compare_btn)
        self.tabs.addTab(page, f"Exact Copies ({len(self._identity.sets)})")

    def _build_divergence_tab(self) -> None:
        page = QWidget(); lay = QVBoxLayout(page)
        warning = QLabel(
            "Identity divergence means one logical Photo currently has Files with different known hashes. "
            "PPA preserves the ambiguity; this screen does not split or repair the identity."
        )
        warning.setWordWrap(True)
        lay.addWidget(warning)
        self.divergence_tree = QTreeWidget()
        self.divergence_tree.setHeaderLabels(["Logical Photo / File", "Current SHA-256", "State"])
        for divergence in self._identity.divergences:
            top = QTreeWidgetItem([f"Logical Photo {_short(divergence.photo_id)}",
                                   f"{len(divergence.known_hashes)} distinct hashes", "REVIEW REQUIRED"])
            top.setData(0, _PHOTO_ID_ROLE, divergence.photo_id)
            self.divergence_tree.addTopLevelItem(top)
            placeholders = ",".join("?" for _ in divergence.file_ids)
            rows = self._conn.execute(
                f"SELECT id,filename,sha256,presence_status,health_status FROM files WHERE id IN ({placeholders}) "
                "ORDER BY sha256,filename COLLATE NOCASE,id", tuple(divergence.file_ids),
            ).fetchall() if divergence.file_ids else []
            for row in rows:
                child = QTreeWidgetItem([row["filename"], _short(row["sha256"], 18),
                                         f"{row['presence_status']} · {row['health_status']}"])
                child.setData(0, _FILE_ID_ROLE, row["id"])
                child.setData(0, _PHOTO_ID_ROLE, divergence.photo_id)
                top.addChild(child)
            top.setExpanded(True)
        lay.addWidget(self.divergence_tree, 1)
        row = QHBoxLayout()
        compare = QPushButton("Compare selected divergent files…")
        compare.clicked.connect(lambda: self._compare_tree(self.divergence_tree))
        self.investigate_btn = QPushButton("Investigate selected divergence…")
        self.investigate_btn.clicked.connect(self._investigate_selected_divergence)
        self.split_identity_btn = QPushButton("Split selected hash cohort…")
        self.split_identity_btn.clicked.connect(self._split_selected_hash_cohort)
        row.addWidget(compare); row.addWidget(self.investigate_btn); row.addWidget(self.split_identity_btn); row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, f"Identity Divergence ({len(self._identity.divergences)})")


    def _investigate_selected_divergence(self) -> None:
        selected = self.divergence_tree.selectedItems()
        photo_ids = []
        for item in selected:
            pid = item.data(0, _PHOTO_ID_ROLE)
            if pid and pid not in photo_ids:
                photo_ids.append(pid)
        if len(photo_ids) != 1:
            QMessageBox.information(self, "Investigate Divergence", "Select one divergent logical Photo or one of its File rows first.")
            return
        try:
            investigation = investigate_identity_divergence(self._conn, library_id=self._library_id, photo_id=photo_ids[0])
        except Exception as exc:
            QMessageBox.warning(self, "Investigate Divergence", str(exc))
            return
        dialog = DivergenceInvestigationDialog(investigation, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show()
        self._child_windows.append(dialog)

    def _split_selected_hash_cohort(self) -> None:
        selected = self.divergence_tree.selectedItems()
        file_ids = self._selected_file_ids(self.divergence_tree)
        photo_ids = []
        for item in selected:
            pid = item.data(0, _PHOTO_ID_ROLE)
            if pid and pid not in photo_ids:
                photo_ids.append(pid)
        if len(photo_ids) != 1 or not file_ids:
            QMessageBox.information(
                self, "Controlled Identity Split",
                "Select one or more physical File rows from exactly one divergent logical Photo. "
                "All selected Files must form the complete current-SHA cohort.")
            return
        try:
            plan = plan_identity_split(
                self._conn, library_id=self._library_id, source_photo_id=photo_ids[0], file_ids=tuple(file_ids))
        except Exception as exc:
            QMessageBox.warning(self, "Controlled Identity Split", str(exc)); return
        names = ", ".join(f.filename for f in plan.files)
        message = (
            f"Create a new logical Photo and move {len(plan.files)} physical File(s) into it?\n\n"
            f"SHA-256: {_short(plan.sha256, 24)}\nFiles: {names}\n"
            f"{plan.remaining_file_count} physical File(s) will remain on the source Photo.\n\n"
            "This changes catalogue identity only. No source file, EXIF, chronology, Event, Album or Tag is rewritten. "
            "The operation is revalidated under a write lock before commit and is append-audited.")
        if QMessageBox.question(self, "Confirm Controlled Identity Split", message) != QMessageBox.StandardButton.Yes:
            return
        try:
            result = execute_identity_split(self._conn, plan, note="Human-confirmed from Duplicate & Lineage review UI")
        except Exception as exc:
            QMessageBox.warning(self, "Controlled Identity Split", str(exc)); return
        QMessageBox.information(
            self, "Identity Split Complete",
            f"Created a new logical Photo and moved {len(result.moved_file_ids)} physical File(s).\n"
            "Reopen Duplicates & Lineage to review the refreshed identity projection.")
        self.accept()


    def _build_resolution_tab(self) -> None:
        page = QWidget(); lay = QVBoxLayout(page)
        note = QLabel(
            "Review controlled identity splits and their current topology. Recombine is offered only when "
            "the original split remains provably reversible and no later identity-dependent curation makes it ambiguous."
        )
        note.setWordWrap(True); lay.addWidget(note)
        self.resolution_table = QTableWidget(0, 6)
        self.resolution_table.setHorizontalHeaderLabels(["When", "Source Photo", "Split-created Photo", "SHA-256 cohort", "Recovery", "Reason"])
        self.resolution_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.resolution_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.resolution_table.horizontalHeader().setStretchLastSection(True)
        self.resolution_table.itemSelectionChanged.connect(self._resolution_selection_changed)
        lay.addWidget(self.resolution_table, 1)
        row = QHBoxLayout()
        self.inspect_resolution_btn = QPushButton("Inspect resolution…")
        self.inspect_resolution_btn.clicked.connect(self._inspect_resolution)
        self.recombine_resolution_btn = QPushButton("Recombine split…")
        self.recombine_resolution_btn.clicked.connect(self._recombine_resolution)
        row.addWidget(self.inspect_resolution_btn); row.addWidget(self.recombine_resolution_btn); row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, "Resolution History")
        self._refresh_resolutions()

    def _library_resolution_rows(self):
        rows = list_identity_resolutions(self._conn)
        return [r for r in rows if int(r['library_id']) == int(self._library_id)]

    def _refresh_resolutions(self) -> None:
        rows = self._library_resolution_rows()
        self.resolution_table.setRowCount(len(rows))
        for i, row in enumerate(reversed(rows)):
            try:
                review = review_identity_resolution(self._conn, row['resolution_id'])
                recovery = "Eligible" if review.recovery_eligible else ("Recombined" if review.recovered else "Review only")
                reason = review.recovery_reason
            except Exception as exc:
                recovery = "Review only"; reason = str(exc)
            vals = [row['created_at'], _short(row['source_photo_id']), _short(row['new_photo_id']),
                    _short(row['sha256'], 18), recovery, reason]
            for col, value in enumerate(vals):
                item = QTableWidgetItem(str(value))
                if col == 0:
                    item.setData(_LINEAGE_ID_ROLE, row['resolution_id'])
                self.resolution_table.setItem(i, col, item)
        self.resolution_table.resizeColumnsToContents()
        self._resolution_selection_changed()

    def _selected_resolution_id(self) -> str | None:
        rows = self.resolution_table.selectionModel().selectedRows() if self.resolution_table.selectionModel() else []
        if not rows:
            return None
        item = self.resolution_table.item(rows[0].row(), 0)
        return item.data(_LINEAGE_ID_ROLE) if item else None

    def _resolution_selection_changed(self) -> None:
        rid = self._selected_resolution_id()
        eligible = False
        if rid:
            try:
                eligible = review_identity_resolution(self._conn, rid).recovery_eligible
            except Exception:
                pass
        self.inspect_resolution_btn.setEnabled(bool(rid))
        self.recombine_resolution_btn.setEnabled(eligible)

    def _inspect_resolution(self) -> None:
        rid = self._selected_resolution_id()
        if not rid:
            return
        try:
            review = review_identity_resolution(self._conn, rid)
        except Exception as exc:
            QMessageBox.warning(self, "Identity Resolution", str(exc)); return
        dialog = IdentityResolutionReviewDialog(review, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show(); self._child_windows.append(dialog)

    def _recombine_resolution(self) -> None:
        rid = self._selected_resolution_id()
        if not rid:
            return
        try:
            plan = plan_identity_recovery(self._conn, rid)
        except Exception as exc:
            QMessageBox.warning(self, "Recombine Identity Split", str(exc)); self._refresh_resolutions(); return
        if QMessageBox.question(
            self, "Confirm Identity Recombination",
            f"Recombine {len(plan.moved_file_ids)} physical File(s) into the original logical Photo?\n\n"
            "This reverses only the audited split. It does not move or rewrite source files. "
            "PPA will revalidate the complete recovery plan under a write lock before commit."
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            execute_identity_recovery(self._conn, plan, note="Human-confirmed from Identity Resolution Review UI")
        except Exception as exc:
            QMessageBox.warning(self, "Recombine Identity Split", str(exc)); self._refresh_resolutions(); return
        QMessageBox.information(self, "Identity Recombined", "The audited split was safely recombined. The original split history remains preserved.")
        self.accept()


    def _build_health_tab(self) -> None:
        page = QWidget(); lay = QVBoxLayout(page)
        note = QLabel(
            "Read-only identity triage. Priority explains what deserves review first; all corrections still use the controlled investigation, split, and recovery workflows."
        )
        note.setWordWrap(True); lay.addWidget(note)
        self.identity_health_table = QTableWidget(0, 5)
        self.identity_health_table.setHorizontalHeaderLabels(["Priority", "Issue", "Status", "Summary", "Next safe action"])
        self.identity_health_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.identity_health_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.identity_health_table.horizontalHeader().setStretchLastSection(True)
        labels={0:"P0",1:"P1",2:"P2",3:"P3",4:"P4",5:"INFO"}
        items=self._health.items
        self.identity_health_table.setRowCount(len(items))
        for row,item in enumerate(items):
            vals=[labels.get(item.priority,str(item.priority)), item.kind.replace("_"," ").title(),
                  item.status.replace("_"," ").title(), item.summary, item.next_action]
            for col,value in enumerate(vals):
                cell=QTableWidgetItem(str(value))
                if col==0:
                    if item.resolution_id:
                        cell.setData(_LINEAGE_ID_ROLE,item.resolution_id)
                    if item.sha256:
                        cell.setData(_SHA_ROLE,item.sha256)
                self.identity_health_table.setItem(row,col,cell)
        self.identity_health_table.resizeColumnsToContents()
        lay.addWidget(self.identity_health_table,1)
        actions = QHBoxLayout()
        self.investigate_competing_btn = QPushButton("Investigate competing identity…")
        self.investigate_competing_btn.clicked.connect(self._investigate_competing_identity)
        actions.addWidget(self.investigate_competing_btn); actions.addStretch(1); lay.addLayout(actions)
        summary=QLabel(
            f"{self._health.integrity_resolution_required_count} integrity-blocked · "
            f"{self._health.competing_identity_count} competing identity · "
            f"{self._health.divergence_count} divergence · "
            f"{self._health.recoverable_split_count} recoverable split · "
            f"{self._health.review_only_split_count} review-only split · "
            f"{self._health.recovered_split_count} recombined"
        )
        summary.setWordWrap(True); lay.addWidget(summary)
        self.tabs.addTab(page, f"Identity Health ({len(items)})")

    def _investigate_competing_identity(self) -> None:
        rows = self.identity_health_table.selectionModel().selectedRows() if self.identity_health_table.selectionModel() else []
        if not rows:
            QMessageBox.information(self, "Competing Identity Investigation", "Select one P1 Competing Identity row first.")
            return
        cell = self.identity_health_table.item(rows[0].row(), 0)
        sha = cell.data(_SHA_ROLE) if cell else None
        if not sha:
            QMessageBox.information(self, "Competing Identity Investigation", "The selected health item is not a P1 competing-identity case.")
            return
        try:
            investigation = investigate_competing_identity(self._conn, library_id=self._library_id, sha256=sha)
        except Exception as exc:
            QMessageBox.warning(self, "Competing Identity Investigation", str(exc)); return
        dialog = CompetingIdentityInvestigationDialog(investigation, self, conn=self._conn)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show(); self._child_windows.append(dialog)


    def _build_lineage_tab(self) -> None:
        page = QWidget(); lay = QVBoxLayout(page)
        self.lineage_table = QTableWidget(0, 5)
        self.lineage_table.setHorizontalHeaderLabels(["Parent", "Relationship", "Child", "Note", "Created"])
        self.lineage_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lineage_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.lineage_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.lineage_table, 1)
        row = QHBoxLayout()
        self.add_lineage_btn = QPushButton("Add lineage…")
        self.add_lineage_btn.clicked.connect(self._add_lineage)
        self.remove_lineage_btn = QPushButton("Remove selected lineage…")
        self.remove_lineage_btn.clicked.connect(self._remove_lineage)
        row.addWidget(self.add_lineage_btn); row.addWidget(self.remove_lineage_btn); row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, "Photo Lineage")
        self._refresh_lineage()

    def _selected_file_ids(self, tree: QTreeWidget) -> list[str]:
        ids: list[str] = []
        for item in tree.selectedItems():
            file_id = item.data(0, _FILE_ID_ROLE)
            if file_id and file_id not in ids:
                ids.append(file_id)
        return ids

    def _compare_selected(self) -> None:
        ids = self._selected_file_ids(self.exact_tree)
        if len(ids) != 2:
            QMessageBox.information(self, "Compare Exact Copies", "Select exactly two physical File rows to compare.")
            return
        try:
            validate_exact_copy_pair(self._conn, library_id=self._library_id, file_ids=tuple(ids))
        except ValueError as exc:
            QMessageBox.warning(self, "Compare Exact Copies", str(exc))
            return
        self._open_compare(ids)

    def _compare_tree(self, tree: QTreeWidget) -> None:
        ids = self._selected_file_ids(tree)
        if len(ids) != 2:
            QMessageBox.information(self, "Compare Files", "Select exactly two physical File rows to compare.")
            return
        self._open_compare(ids)

    def _open_compare(self, ids: list[str]) -> None:
        dialog = SideBySidePreviewDialog(self._conn, ids[0], ids[1], self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show()
        self._child_windows.append(dialog)

    def _refresh_lineage(self) -> None:
        relevant = _library_photo_ids(self._conn, self._library_id)
        relations = [r for r in list_lineage(self._conn)
                     if r.parent_photo_id in relevant or r.child_photo_id in relevant]
        labels = dict(_photo_choices(self._conn, self._library_id))
        self.lineage_table.setRowCount(len(relations))
        for i, rel in enumerate(relations):
            parent = labels.get(rel.parent_photo_id, f"Photo {_short(rel.parent_photo_id)}")
            child = labels.get(rel.child_photo_id, f"Photo {_short(rel.child_photo_id)}")
            vals = [parent, rel.relation_type.replace("_", " "), child, rel.note or "", rel.created_at]
            for col, value in enumerate(vals):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(_LINEAGE_ID_ROLE, rel.id)
                self.lineage_table.setItem(i, col, item)
        self.lineage_table.resizeColumnsToContents()

    def _selected_lineage_id(self) -> str | None:
        rows = self.lineage_table.selectionModel().selectedRows() if self.lineage_table.selectionModel() else []
        if not rows:
            return None
        item = self.lineage_table.item(rows[0].row(), 0)
        return item.data(_LINEAGE_ID_ROLE) if item else None

    def _add_lineage(self) -> None:
        dialog = AddLineageDialog(self._conn, self._library_id, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        parent, child, relation, note = dialog.values()
        try:
            add_lineage(self._conn, parent_photo_id=parent, child_photo_id=child,
                        relation_type=relation, note=note)
        except Exception as exc:
            QMessageBox.warning(self, "Add Photo Lineage", str(exc))
            return
        self._refresh_lineage()

    def _remove_lineage(self) -> None:
        lineage_id = self._selected_lineage_id()
        if not lineage_id:
            QMessageBox.information(self, "Remove Photo Lineage", "Select one lineage row first.")
            return
        if QMessageBox.question(
            self, "Remove Photo Lineage",
            "Remove this active lineage relationship? The append-only lineage history will be preserved.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            remove_lineage(self._conn, lineage_id)
        except Exception as exc:
            QMessageBox.warning(self, "Remove Photo Lineage", str(exc))
            return
        self._refresh_lineage()
