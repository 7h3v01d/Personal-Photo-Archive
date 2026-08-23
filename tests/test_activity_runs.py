import json
from pathlib import Path
from types import SimpleNamespace

from ppa.activity_runs import load_activity_runs, export_run_transcript, RUN_TRANSCRIPT_SCHEMA


def _write(path: Path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_runs_group_and_terminal_outcome(tmp_path):
    log = tmp_path / "ppa.log"
    js = tmp_path / "ppa.jsonl"
    _write(js, [
        {"timestamp":"2026-01-01T00:00:00+00:00","message":"start","run_id":"abc","operation":"date_review","run_phase":"start"},
        {"timestamp":"2026-01-01T00:00:01+00:00","message":"analyse","run_id":"abc","operation":"date_review","run_phase":"progress"},
        {"timestamp":"2026-01-01T00:00:02+00:00","message":"done","run_id":"abc","operation":"date_review","run_phase":"end","run_outcome":"success","elapsed_ms":2000},
    ])
    runs = load_activity_runs(log)
    assert len(runs) == 1
    assert runs[0].outcome == "success"
    assert runs[0].elapsed_ms == 2000
    assert len(runs[0].events) == 3


def test_incomplete_run_is_running(tmp_path):
    log = tmp_path / "ppa.log"
    _write(tmp_path / "ppa.jsonl", [
        {"timestamp":"2026-01-01T00:00:00+00:00","message":"start","run_id":"abc","operation":"scan","run_phase":"start"},
    ])
    assert load_activity_runs(log)[0].outcome == "running"


def test_export_run_redacts_private_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    log = tmp_path / "ppa.log"
    private = str(home / "Pictures" / "secret.jpg")
    _write(tmp_path / "ppa.jsonl", [
        {"timestamp":"2026-01-01T00:00:00+00:00","message":f"reading {private}","run_id":"abc","operation":"scan","run_phase":"start"},
        {"timestamp":"2026-01-01T00:00:01+00:00","message":"done","run_id":"abc","operation":"scan","run_phase":"end","run_outcome":"success","elapsed_ms":1000},
    ])
    cfg = SimpleNamespace(log_path=log, db_path=tmp_path/'missing.sqlite')
    out = export_run_transcript(cfg, "abc", tmp_path / "run")
    text = out.read_text(encoding="utf-8")
    assert RUN_TRANSCRIPT_SCHEMA in text
    assert private not in text
    assert "<HOME>" in text


def test_rotated_logs_are_correlated(tmp_path):
    log=tmp_path/'ppa.log'
    _write(tmp_path/'ppa.jsonl.1', [{"timestamp":"2026-01-01T00:00:00+00:00","message":"start","run_id":"abc","operation":"scan","run_phase":"start"}])
    _write(tmp_path/'ppa.jsonl', [{"timestamp":"2026-01-01T00:00:02+00:00","message":"end","run_id":"abc","operation":"scan","run_phase":"end","run_outcome":"success","elapsed_ms":2000}])
    runs=load_activity_runs(log)
    assert len(runs)==1 and len(runs[0].events)==2

def test_json_formatter_carries_run_fields():
    import logging
    from ppa.logging_setup import JsonLinesFormatter
    record = logging.LogRecord("ppa.test", logging.INFO, __file__, 1, "hello", (), None)
    record.run_id = "abc"
    record.operation = "pilot_audit"
    record.run_phase = "end"
    record.run_outcome = "success"
    record.elapsed_ms = 42
    record.run_detail = {"usable": 7}
    obj = json.loads(JsonLinesFormatter().format(record))
    assert obj["run_id"] == "abc"
    assert obj["operation"] == "pilot_audit"
    assert obj["run_phase"] == "end"
    assert obj["run_outcome"] == "success"
    assert obj["elapsed_ms"] == 42
    assert obj["run_detail"] == {"usable": 7}
