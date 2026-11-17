"""Fast regression checks for optional population-weighted timing/logging."""
from argparse import Namespace
from contextlib import contextmanager
import re

import pytest

from net_distance_ph import population_routing as routing
from net_distance_ph.population_weighted import parse_args, run_options


def test_population_weighted_accepts_timing_interval():
    assert parse_args(["--heartbeat-seconds", "1.5"]).heartbeat_seconds == 1.5
    assert parse_args([]).heartbeat_seconds == 30.0


def test_timing_helper_preserves_results_and_reports_failure(monkeypatch):
    events = []

    @contextmanager
    def timed_step(label, heartbeat_seconds=30):
        events.append(("start", label, heartbeat_seconds))
        try:
            yield
        except Exception:
            events.append(("fail", label, heartbeat_seconds))
            raise
        else:
            events.append(("done", label, heartbeat_seconds))

    monkeypatch.setattr(routing, "_pw_timed_step", timed_step)
    token = routing._PW_HEARTBEAT_SECONDS.set(1.25)
    try:
        assert routing._pw_time_call("dataset", lambda x, y=2: x + y, 3, y=4) == 7

        @routing._pw_timed_phase("selection")
        def fail():
            raise RuntimeError("expected")

        with pytest.raises(RuntimeError, match="expected"):
            fail()
    finally:
        routing._PW_HEARTBEAT_SECONDS.reset(token)
    assert events == [
        ("start", "dataset", 1.25), ("done", "dataset", 1.25),
        ("start", "selection", 1.25), ("fail", "selection", 1.25),
    ]


def test_population_weighted_persists_timestamps_and_duration(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(routing, "run", lambda args: print("synthetic routing event") or 0)
    assert run_options(Namespace(workspace=tmp_path, heartbeat_seconds=30.0)) == 0
    output = capsys.readouterr().out
    assert re.search(r"\[\d{2}:\d{2}:\d{2}\] START: population-weighted", output)
    assert "DONE: population-weighted (total); elapsed" in output
    logs = list((tmp_path / "logs").glob("run-*.log"))
    assert len(logs) == 1
    content = logs[0].read_text(encoding="utf-8")
    assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", content)
    assert "synthetic routing event" in content
    assert "DONE: population-weighted (total); elapsed" in content


def test_population_weighted_rejects_bad_heartbeat(tmp_path):
    with pytest.raises(ValueError, match="heartbeat-seconds"):
        run_options(Namespace(workspace=tmp_path, heartbeat_seconds=0))
