from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from ppa.config import Config
from ppa.diagnostics import export_diagnostics, sanitize_text, structured_log_path, tail_text
from ppa.logging_setup import configure_logging, get_logger
from ppa.db import connect
from ppa.scanner import scan_library
from ppa.safe_export import enroll_export_root


def _config(tmp_path: Path) -> Config:
    db = tmp_path / "data" / "catalogue.sqlite3"
    db.parent.mkdir(parents=True)
    lib = tmp_path / "photos" / "family"
    lib.mkdir(parents=True)
    conn = connect(db)
    scan_library(conn, lib)
    conn.close()
    return Config(db_path=db, log_level="INFO", log_path=tmp_path / "data" / "logs" / "ppa.log", library_directories=[])


def test_logging_writes_text_and_structured_jsonl(tmp_path):
    cfg = _config(tmp_path)
    configure_logging(cfg.log_path, "INFO")
    get_logger("test").info("hello diagnostics")
    for h in logging.getLogger("ppa").handlers:
        h.flush()
    assert "hello diagnostics" in cfg.log_path.read_text(encoding="utf-8")
    rows = structured_log_path(cfg.log_path).read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[-1])
    assert payload["message"] == "hello diagnostics"
    assert payload["logger"] == "ppa.test"
    assert payload["level"] == "INFO"


def test_tail_text_returns_only_requested_tail(tmp_path):
    path = tmp_path / "a.log"
    path.write_text("1\n2\n3\n4\n", encoding="utf-8")
    assert tail_text(path, lines=2) == "3\n4\n"


def test_sanitize_text_redacts_windows_slash_variants():
    pairs = [(r"C:\\Users\\Leon\\Pictures", "<LIBRARY_1>")]
    assert "<LIBRARY_1>" in sanitize_text(r"bad C:\\Users\\Leon\\Pictures\\x.jpg", pairs)


def test_export_is_sanitized_and_excludes_db_and_photo_content(tmp_path):
    cfg = _config(tmp_path)
    configure_logging(cfg.log_path, "INFO")
    secret_path = tmp_path / "photos" / "family" / "private" / "IMG_001.jpg"
    get_logger("test").warning("Could not read %s", secret_path)
    for h in logging.getLogger("ppa").handlers:
        h.flush()
    photo = secret_path
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"SECRET_PHOTO_BYTES")

    _c = connect(cfg.db_path); enroll_export_root(tmp_path, conn=_c, config=cfg); _c.close()
    out = export_diagnostics(cfg, tmp_path / "share.zip")
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert not any(name.endswith("catalogue.sqlite3") for name in names)
        assert not any(name.lower().endswith(".jpg") for name in names)
        combined = "\n".join(zf.read(n).decode("utf-8", errors="replace") for n in names)
        assert str(secret_path) not in combined
        assert "SECRET_PHOTO_BYTES" not in combined
        assert "<LIBRARY_1>" in combined
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["schema"] == "ppa-diagnostics/1"
        assert "catalogue database" in manifest["explicit_exclusions"]
