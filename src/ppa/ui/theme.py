"""Dark industrial theme.

Flat, zero-radius, monospace. Obsidian ground with a teal accent, phosphor
green for healthy/active state and amber for anything that wants attention
(missing files, hash mismatches, duplicates). Colours are exposed as
constants so widgets can tint status without hard-coding hex all over.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# Core palette
OBSIDIAN = "#0b0f14"   # window ground
PANEL = "#11161d"      # raised panels
PANEL_ALT = "#0f141a"  # inset / list backgrounds
BORDER = "#1e2831"     # hairlines
TEAL = "#2fd6c3"       # primary accent / selection / focus
PHOSPHOR = "#4be08a"   # positive / active
AMBER = "#ffb454"      # warning / attention
RED = "#ff6b6b"        # error / missing
TEXT = "#c8d3da"       # primary text
TEXT_DIM = "#6b7a85"   # secondary text

FONT_FAMILY = "JetBrains Mono, DejaVu Sans Mono, Consolas, monospace"

# Status -> accent colour, used by the grid/inspector for tinting.
STATUS_COLOURS = {
    "active": PHOSPHOR,
    "moved": TEAL,
    "duplicate": AMBER,
    "missing": RED,
    "error": RED,
}


def status_colour(status: str) -> str:
    return STATUS_COLOURS.get(status, TEXT_DIM)


_QSS = f"""
* {{
    font-family: {FONT_FAMILY};
    font-size: 13px;
    color: {TEXT};
}}

QMainWindow, QWidget {{
    background: {OBSIDIAN};
}}

QToolBar {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 4px;
    spacing: 4px;
}}

QToolButton, QPushButton {{
    background: {PANEL_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 0px;
    padding: 6px 12px;
}}
QToolButton:hover, QPushButton:hover {{
    border: 1px solid {TEAL};
    color: {TEAL};
}}
QToolButton:pressed, QPushButton:pressed {{
    background: {OBSIDIAN};
}}
QToolButton:disabled, QPushButton:disabled {{
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
}}

QSplitter::handle {{
    background: {BORDER};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QListView, QListWidget {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    border-radius: 0px;
    outline: 0;
}}
QListView::item, QListWidget::item {{
    color: {TEXT};
    padding: 6px 10px;
    border: 0px;
}}
QListWidget::item:selected {{
    background: {PANEL};
    color: {TEAL};
    border-left: 2px solid {TEAL};
}}
QListView::item:selected {{
    background: {PANEL};
    border: 1px solid {TEAL};
}}

QLabel#SectionHeader {{
    color: {TEXT_DIM};
    font-size: 11px;
    letter-spacing: 1px;
    padding: 8px 4px 4px 4px;
}}
QLabel#InspectorTitle {{
    color: {TEAL};
    font-size: 15px;
}}
QLabel#FieldKey {{
    color: {TEXT_DIM};
}}
QLabel#FieldVal {{
    color: {TEXT};
}}

QStatusBar {{
    background: {PANEL};
    border-top: 1px solid {BORDER};
    color: {TEXT_DIM};
}}
QStatusBar::item {{ border: none; }}

QScrollBar:vertical {{
    background: {OBSIDIAN};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    min-height: 30px;
    border-radius: 0px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEAL}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QFrame#Divider {{ background: {BORDER}; max-height: 1px; min-height: 1px; }}
"""


def apply_theme(app: QApplication) -> None:
    """Apply the dark industrial theme to a running QApplication."""
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(OBSIDIAN))
    pal.setColor(QPalette.Base, QColor(PANEL_ALT))
    pal.setColor(QPalette.AlternateBase, QColor(PANEL))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(PANEL))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(TEAL))
    pal.setColor(QPalette.HighlightedText, QColor(OBSIDIAN))
    pal.setColor(QPalette.ToolTipBase, QColor(PANEL))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    app.setPalette(pal)

    app.setFont(QFont("JetBrains Mono"))
    app.setStyleSheet(_QSS)
