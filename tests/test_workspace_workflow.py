"""Offline tests for workspace layout, source reuse and timestamped logging."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys

import pytest
from net_distance_ph import workspace as ws


PROJECT = Path(__file__).resolve().parents[1]


def _publish(root, pointer_name, entries):
    directory = root / ("boundary_releases" if pointer_name == "current_boundaries.json" else "transport_releases") / "test-release"
    directory.mkdir(parents=True)
    record = {}
    for key, filename in entries.items():
        path = directory / filename
        for ext in (".shp", ".shx", ".dbf"):
            path.with_suffix(ext).write_bytes(b"fixture")
        record[key] = path.relative_to(root).as_posix()
    (root / pointer_name).write_text(json.dumps(record), encoding="utf-8")


def test_workspace_defaults_to_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("NET_DISTANCE_PH_WORKSPACE", raising=False)
    assert ws.workspace_path() == (tmp_path / "Documents" / "net-distance-ph").resolve()
    assert ws.paths(ws.workspace_path())["output"].name == "output"


def test_workspace_override(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_DISTANCE_PH_WORKSPACE", str(tmp_path / "from-env"))
    assert ws.workspace_path() == (tmp_path / "from-env").resolve()
    assert ws.workspace_path(tmp_path / "from-cli") == (tmp_path / "from-cli").resolve()


def test_prepare_reuses_release_without_building(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _publish(data, "current_boundaries.json", {"centroids": "muncity_centroids.shp"})
    _publish(data, "current_transport.json", {"roads": "primsectertrunk_road.shp", "ferries": "ferry_route_shape.shp"})
    monkeypatch.setattr(ws, "_build_boundaries", lambda *_: pytest.fail("should reuse boundaries"))
    monkeypatch.setattr(ws, "_build_transport", lambda *_: pytest.fail("should reuse transport"))
    ws.prepare_sources(data)


def test_prepare_missing_both_sources_in_order(tmp_path, monkeypatch):
    data = tmp_path / "data"
    built = []
    def boundaries(root):
        built.append("boundaries")
        _publish(root, "current_boundaries.json", {"centroids": "muncity_centroids.shp"})
    def transport(root):
        built.append("transport")
        _publish(root, "current_transport.json", {"roads": "primsectertrunk_road.shp", "ferries": "ferry_route_shape.shp"})
    monkeypatch.setattr(ws, "_build_boundaries", boundaries)
    monkeypatch.setattr(ws, "_build_transport", transport)
    ws.prepare_sources(data)
    assert built == ["boundaries", "transport"]


def test_prepare_orders_builders_and_refuses_incomplete_boundary(tmp_path, monkeypatch):
    data = tmp_path / "data"
    def fail_boundaries(_):
        raise RuntimeError("unmatched locality")
    monkeypatch.setattr(ws, "_build_boundaries", fail_boundaries)
    monkeypatch.setattr(ws, "_build_transport", lambda *_: pytest.fail("transport must not start"))
    with pytest.raises(RuntimeError, match="unmatched locality"):
        ws.prepare_sources(data)


def test_prepare_publishes_only_needed_layers(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _publish(data, "current_boundaries.json", {"centroids": "muncity_centroids.shp"})
    events = []
    monkeypatch.setattr(ws, "_build_boundaries", lambda *_: pytest.fail("should reuse boundaries"))
    def build_transport(root):
        events.append("transport")
        _publish(root, "current_transport.json", {"roads": "primsectertrunk_road.shp", "ferries": "ferry_route_shape.shp"})
    monkeypatch.setattr(ws, "_build_transport", build_transport)
    ws.prepare_sources(data)
    assert events == ["transport"]


def test_invalid_pointer_is_not_replaced(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    pointer = data / "current_boundaries.json"
    pointer.write_text('{"centroids": "../outside/muncity_centroids.shp"}', encoding="utf-8")
    monkeypatch.setattr(ws, "_build_boundaries", lambda *_: pytest.fail("must not overwrite pointer"))
    with pytest.raises(RuntimeError, match="invalid"):
        ws.prepare_sources(data)
    assert pointer.exists()


def test_run_log_prefixes_each_line_and_preserves_stdout(tmp_path, capsys):
    original = sys.stdout
    with ws.run_log(tmp_path) as logfile:
        print("one\ntwo")
        print("warning", file=sys.stderr)
    assert sys.stdout is original
    text = logfile.read_text(encoding="utf-8")
    assert "[INFO] one" in text and "[INFO] two" in text
    assert "[ERROR] warning" in text
    assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\] \[INFO\] one", text)


def test_legacy_network_layers_do_not_require_attribute_tables(tmp_path, monkeypatch):
    """Road/ferry DBFs and SHXs are optional in the existing core reader."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "primsectertrunk_road.shp").write_bytes(b"geometry")
    (data / "ferry_route_shape.shp").write_bytes(b"geometry")
    (data / "muncity_centroids.shp").write_bytes(b"points")
    (data / "muncity_centroids.dbf").write_bytes(b"IDs and names")
    monkeypatch.setattr(ws, "_build_boundaries", lambda *_: pytest.fail("no download expected"))
    monkeypatch.setattr(ws, "_build_transport", lambda *_: pytest.fail("no download expected"))
    ws.prepare_sources(data)


def test_legacy_centroid_requires_dbf(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "muncity_centroids.shp").write_bytes(b"points")
    with pytest.raises(RuntimeError, match="Partial or ambiguous legacy centroids"):
        ws._selected(data, "current_boundaries.json", {"centroids": "muncity_centroids.shp"})


def test_legacy_multiple_road_layers_remain_ambiguous(tmp_path):
    data = tmp_path / "data"
    for place in (data / "source_a", data / "source_b"):
        place.mkdir(parents=True)
        (place / "primsectertrunk_road.shp").write_bytes(b"geometry")
    with pytest.raises(RuntimeError, match="Partial or ambiguous legacy roads"):
        ws._selected(data, "current_transport.json", {"roads": "primsectertrunk_road.shp"})


def test_cli_workspace_runs_with_prepared_legacy_inputs(tmp_path):
    """The no-`--data` CLI uses its own workspace, without network access."""
    from tests.test_end_to_end import build_fixture
    root = tmp_path / "workspace"
    build_fixture(root / "data")
    env = {key: value for key, value in os.environ.items() if not key.startswith("NET_DISTANCE_PH_")}
    result = subprocess.run(
        [sys.executable, str(PROJECT / "run.py"), "all", "--workspace", str(root),
         "--workers", "1"], cwd=PROJECT, env=env, text=True, capture_output=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert (root / "output" / "distance_matrix_km.npy").exists()
    assert (root / "work" / "sources.npz").exists()
    logs = list((root / "logs").glob("run-*.log"))
    assert len(logs) == 1
    assert "START: graph" in logs[0].read_text(encoding="utf-8")
    assert "DONE: verify" in logs[0].read_text(encoding="utf-8")
