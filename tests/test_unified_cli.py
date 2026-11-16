"""Contract tests for the unified net-distance-ph entry point.

These tests use temporary directories and mocked subprocesses only. They do
not download GIS files, construct a national graph, or claim empirical results.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from net_distance_ph import cli


def test_help_is_an_installed_app(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--help"])
    assert exc.value.code == 0
    printed = capsys.readouterr().out
    assert "net-distance-ph" in printed
    for name in ("doctor", "status", *cli.STAGES, "run"):
        assert name in printed


@pytest.mark.parametrize("args,expected", [
    (["run", "--until", "connectivity"], ("prepare", "graph", "connectivity")),
    (["run", "--until", "prepare"], ("prepare",)),
    (["run", "--from", "reduce"], ("reduce", "matrix", "verify")),
    (["run", "--only", "matrix"], ("matrix",)),
    (["run", "--all"], cli.STAGES),
    (["graph"], ("graph",)),
])
def test_run_plans(args, expected):
    parsed = cli.build_parser().parse_args(args)
    assert cli._selection(parsed) == expected


def test_run_needs_selector():
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["run"])
    assert exc.value.code == 2


def test_unknown_stage_fails():
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["giraffe"])
    assert exc.value.code == 2


def test_run_exact_until_connectivity(tmp_path, monkeypatch):
    commands = []
    def fake_run(argv, **kwargs):
        commands.append((argv, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["run", "--until", "connectivity", "--workspace", str(tmp_path), "--verbose"]) == 0
    assert [c[0][3] for c in commands] == ["prepare", "graph", "connectivity"]
    for argv, kw in commands:
        assert argv[:3] == [cli.sys.executable, "-m", "run"]
        assert argv[argv.index("--workspace") + 1] == str(tmp_path.resolve())
        assert "--verbose" in argv
        assert kw["check"] is False


def test_run_stops_at_failed_connectivity(tmp_path, monkeypatch):
    seen = []
    def fake_run(argv, **kwargs):
        seen.append(argv[3])
        return SimpleNamespace(returncode=1 if argv[3] == "connectivity" else 0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["run", "--all", "--workspace", str(tmp_path)]) == 1
    assert seen == ["prepare", "graph", "connectivity"]


def test_run_stops_at_failed_prepare(tmp_path, monkeypatch):
    seen = []
    def fake_run(argv, **kwargs):
        seen.append(argv[3]); return SimpleNamespace(returncode=2)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["run", "--until", "connectivity", "--workspace", str(tmp_path)]) == 2
    assert seen == ["prepare"]


def test_source_options_propagate_correctly(tmp_path, monkeypatch):
    cmds = []
    def fake_run(argv, **kwargs):
        cmds.append(argv); return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["run", "--from", "reduce", "--workers", "4", "--restart-reduce",
                     "--waves", "1,2", "--heartbeat-seconds", "4", "--workspace", str(tmp_path)]) == 0
    assert [cmd[3] for cmd in cmds] == ["reduce", "matrix", "verify"]
    assert "--restart-reduce" in cmds[0] and "--workers" not in cmds[0]
    assert "--workers" in cmds[1] and cmds[1][cmds[1].index("--workers") + 1] == "4"
    assert "--waves" in cmds[1] and cmds[1][cmds[1].index("--waves") + 1] == "1,2"
    assert "--restart-reduce" not in cmds[1]
    assert "--waves" not in cmds[2]


def test_status_is_read_only_json(tmp_path, capsys):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "graph_full.npz").write_bytes(b"placeholder")
    assert cli.main(["status", "--workspace", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["files"]["graph"]["exists"] is True
    assert report["files"]["locality_attachments"]["exists"] is False
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "output").exists()


def test_status_accepts_nondefault_paths(tmp_path, capsys):
    overrides = [str(tmp_path / name) for name in ("inputs", "graph", "results")]
    assert cli.main(["status", "--data", overrides[0], "--work", overrides[1],
                     "--out", overrides[2], "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["data"] == overrides[0]
    assert report["work"] == overrides[1]
    assert report["output"] == overrides[2]


def test_doctor_stops_cleanly_without_inputs(tmp_path, monkeypatch):
    seen = []
    def fake_run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _: True)
    assert cli.main(["doctor", "--workspace", str(tmp_path)]) == 2
    assert seen[0][:3] == [cli.sys.executable, "-m", "check_setup"]
    assert seen[0][seen[0].index("--data") + 1] == str(tmp_path / "data")


def test_invalid_heartbeat_rejected_before_spawn(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise AssertionError("must not spawn a stage with invalid heartbeat")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["prepare", "--workspace", str(tmp_path), "--heartbeat-seconds", "0"]) == 2


def test_entry_point_and_wheel_inclusion_manifest():
    import tomllib
    project = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    assert cfg["project"]["scripts"]["net-distance-ph"] == "net_distance_ph.cli:main"
    assert "run" in cfg["tool"]["setuptools"]["py-modules"]
    assert "check_setup" in cfg["tool"]["setuptools"]["py-modules"]
    assert "scripts*" in cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "reference*" in cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    assert (project / "reference" / "__init__.py").is_file()
    assert (project / "reference" / "psgc" / "__init__.py").is_file()
