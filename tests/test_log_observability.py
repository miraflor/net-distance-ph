"""Regression tests for compact timezone-free logging and structured events (v47.1)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import time

from net_distance_ph import workspace as ws


STAMP = re.compile(r"^\[\d{2}:\d{2}:\d{2} e=\d+:\d{2}(?::\d{2})?\] ")


def test_timestamp_and_run_clock_identical_in_console_and_file(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("NET_DISTANCE_PH_PIPELINE_STARTED_UTC", raising=False)
    monkeypatch.delenv("NET_DISTANCE_PH_JOURNAL_PATH", raising=False)
    with ws.run_log(tmp_path):
        print("example message")
        with ws.timed_step("tiny test stage", heartbeat_seconds=30):
            print("stage output")
    console = capsys.readouterr().out.splitlines()
    log = list((tmp_path / "logs").glob("run-*.log"))
    assert len(log) == 1
    archive = log[0].read_text(encoding="utf-8").splitlines()
    assert console == archive
    assert console and all(STAMP.match(line) for line in console)
    assert any("START: tiny test stage" in line for line in console)
    assert any("DONE: tiny test stage (" in line for line in console)
    events = [json.loads(line) for line in log[0].with_suffix(".jsonl").read_text().splitlines()]
    assert [entry["event"] for entry in events] == ["phase_start", "phase_finish"]
    assert events[-1]["status"] == "complete"
    assert isinstance(events[-1]["elapsed_seconds"], float)


def test_pipeline_clock_is_inherited(tmp_path, capsys, monkeypatch):
    start = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    monkeypatch.setenv("NET_DISTANCE_PH_PIPELINE_STARTED_UTC", start)
    with ws.run_log(tmp_path):
        print("clock check")
    assert re.search(r"\[\d{2}:\d{2}:\d{2} e=03:00\]", capsys.readouterr().out)


def test_nested_phase_only_deepest_heartbeat_and_journal(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("NET_DISTANCE_PH_PIPELINE_STARTED_UTC", raising=False)
    with ws.run_log(tmp_path):
        with ws.timed_step("outer", heartbeat_seconds=0.01):
            with ws.timed_step("inner", heartbeat_seconds=0.01):
                trace = ws._ACTIVE_TRACE.get()
                assert not trace.current(trace.stack[0][0])
                time.sleep(0.07)
    output = capsys.readouterr().out
    assert "STILL RUNNING: inner" in output
    entries = [json.loads(x) for x in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()]
    assert [x["event"] for x in entries] == ["phase_start", "phase_start", "phase_finish", "phase_finish"]
    assert entries[1]["parent_id"] == entries[0]["phase_id"]


def test_routing_progress_reports_rate_eta_and_structured_event(tmp_path, capsys):
    with ws.run_log(tmp_path):
        ws.progress_event("OD routing", 50, 100, time.monotonic() - 10, unit="new source nodes")
    output = capsys.readouterr().out
    assert "OD routing 50/100 (50%)" in output
    assert "/s" in output
    assert "ETA 00:10" in output
    journal = next((tmp_path / "logs").glob("*.jsonl"))
    record = json.loads(journal.read_text().splitlines()[-1])
    assert record["event"] == "progress"
    assert (record["completed"], record["total"]) == (50, 100)
    assert 9.0 < record["eta_seconds"] < 11.0


def test_failure_is_recorded(tmp_path, capsys):
    try:
        with ws.run_log(tmp_path):
            with ws.timed_step("broken stage", heartbeat_seconds=30):
                raise RuntimeError("synthetic failure")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected error")
    journal = next((tmp_path / "logs").glob("*.jsonl"))
    record = json.loads(journal.read_text().splitlines()[-1])
    assert record["event"] == "phase_failed"
    assert record["error_type"] == "RuntimeError"
    assert record["error"] == "synthetic failure"


def test_reset_runner_preserves_one_journal_under_workspace(tmp_path, monkeypatch):
    from net_distance_ph import reset
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = tmp_path / "net-distance-ph"
    # Simulate each subprocess starting its own instrumented stage; no actual
    # download or graph computation is needed for the integration contract.
    call_no = [0]

    def fake_run(argv, *, cwd=None, env=None, check=False):
        call_no[0] += 1
        assert env["NET_DISTANCE_PH_JOURNAL_PATH"] == str(workspace / "logs" / "fresh_run_journal.jsonl")
        assert "NET_DISTANCE_PH_PIPELINE_STARTED_UTC" in env
        with monkeypatch.context() as m:
            m.setattr(ws.os, "getpid", lambda: 12000 + call_no[0])
            m.setenv("NET_DISTANCE_PH_JOURNAL_PATH", env["NET_DISTANCE_PH_JOURNAL_PATH"])
            m.setenv("NET_DISTANCE_PH_PIPELINE_STARTED_UTC", env["NET_DISTANCE_PH_PIPELINE_STARTED_UTC"])
            with ws.run_log(workspace):
                with ws.timed_step("synthetic child", heartbeat_seconds=30):
                    print("running")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(reset.subprocess, "run", fake_run)
    assert reset._run_from_zero(repo, workspace, branch="test", commit="abc123", workers=2, heartbeat=30) == 0
    summary = json.loads((workspace / "logs" / "fresh_run_summary.json").read_text())
    assert summary["status"] == "complete"
    assert len(summary["stages"]) == 3
    assert len(summary["phases"]) == 3
    assert all(entry["phase"] == "synthetic child" for entry in summary["phases"])
