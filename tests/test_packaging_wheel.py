"""Installed-wheel bootstrap regression for packaged SQL migrations."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


def test_built_wheel_contains_migrations_and_bootstraps_fresh_catalogue(tmp_path: Path) -> None:
    pytest.importorskip("setuptools", minversion="68", reason="project build backend requires setuptools>=68")
    root = Path(__file__).resolve().parents[1]
    build_root = tmp_path / "wheel-source"
    build_root.mkdir()
    shutil.copy2(root / "pyproject.toml", build_root / "pyproject.toml")
    shutil.copytree(root / "src", build_root / "src")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    # Exercise the declared setuptools build backend directly. This keeps the
    # regression offline and avoids making package correctness depend on whether
    # the developer venv happens to have pip's optional wheel command tooling.
    build_code = (
        "from setuptools import build_meta; "
        f"print(build_meta.build_wheel({str(wheelhouse)!r}))"
    )
    subprocess.run(
        [sys.executable, "-c", build_code],
        cwd=build_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wheels = list(wheelhouse.glob("personal_photo_archive-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as zf:
        migrations = sorted(
            name for name in zf.namelist()
            if name.startswith("ppa/db/migrations/") and name.endswith(".sql")
        )
    assert len(migrations) >= 39
    assert migrations[0].endswith("001_initial.sql")
    assert migrations[-1].endswith("039_positive_operational_authority.sql")

    installed = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--disable-pip-version-check",
            "--target",
            str(installed),
            str(wheel),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    db = tmp_path / "wheel-catalogue.sqlite3"
    code = r'''
import json
from pathlib import Path
from ppa.db import connect, current_schema_version
from ppa.db.connection import MIGRATIONS_DIR
conn = connect(Path(__import__("sys").argv[1]))
print(json.dumps({
    "schema": current_schema_version(conn),
    "migration_count": len(list(MIGRATIONS_DIR.glob("*.sql"))),
    "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
    "has_files": conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'").fetchone() is not None,
}))
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed)
    proc = subprocess.run(
        [sys.executable, "-c", code, str(db)],
        cwd=tmp_path,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload == {
        "schema": 39,
        "migration_count": 39,
        "integrity": "ok",
        "has_files": True,
    }
