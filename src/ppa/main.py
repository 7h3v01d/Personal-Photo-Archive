"""Application entry point.

Boots the desktop application: config -> logging -> catalogue database ->
themed Qt window. The window (ppa.ui.main_window.MainWindow) is the real
Phase 4/5 UI — navigation, thumbnail grid, inspector — driving the Phase 1/2
scanner and integrity engine on background threads.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from ppa.config import Config
from ppa.db import connect, current_schema_version
from ppa.logging_setup import configure_logging, get_logger
from ppa.ui.main_window import MainWindow
from ppa.ui.theme import apply_theme


def main() -> int:
    config = Config.load()
    configure_logging(config.log_path, config.log_level)
    log = get_logger("main")
    log.info("Starting Personal Photo Archive")

    # Confirm the catalogue is reachable and initialised before the UI opens.
    conn = connect(config.db_path)
    schema_version = current_schema_version(conn)
    conn.close()
    log.info("Catalogue ready at %s (schema v%s)", config.db_path, schema_version)

    app = QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
