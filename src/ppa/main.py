"""Application entry point.

Phase 0 exit criteria this satisfies: project launches, database
initialises, configuration loads, logging works.

Deliberately just a blank window for now — Phase 4/5 add the real UI
(grid view, folder browser, inspector). This exists so the plumbing
(config -> logging -> db -> Qt event loop) can be verified end to end.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

from ppa.config import Config
from ppa.db import connect, current_schema_version
from ppa.logging_setup import configure_logging, get_logger


class MainWindow(QMainWindow):
    def __init__(self, schema_version: int) -> None:
        super().__init__()
        self.setWindowTitle("Personal Photo Archive")
        self.resize(1000, 700)
        self.setCentralWidget(
            QLabel(f"  Catalogue schema v{schema_version} — foundation online.")
        )


def main() -> int:
    config = Config.load()
    configure_logging(config.log_path, config.log_level)
    log = get_logger("main")
    log.info("Starting Personal Photo Archive")

    conn = connect(config.db_path)
    schema_version = current_schema_version(conn)
    log.info("Catalogue database ready at %s (schema v%s)", config.db_path, schema_version)

    app = QApplication(sys.argv)
    window = MainWindow(schema_version)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
