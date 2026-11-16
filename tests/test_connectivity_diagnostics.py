"""Offline, deterministic tests of read-only national component-gap diagnosis."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from net_distance_ph import cli
from net_distance_ph import connectivity_diagnostics as diag


def fake_graph():
    # Main component has road nodes 0,1,2; component 2 is a distinct
    # parallel tiny road stub 11 m away; component 3 is far away.
    nodes = np.array([
        [120.0, 10.0], [120.001, 10.0], [120.002, 10.0],
        [120.0011, 10.0], [120.0012, 10.0],
        [121.0, 11.0], [121.001, 11.0],
    ])
    flags = np.zeros((len(nodes), 3), dtype=bool)
    flags[:, 0] = True
    return {"node_lonlat": nodes, "flags": flags,
            "u": np.array([0, 1, 3, 5]), "v": np.array([1, 2, 4, 6]),
            "code": np.zeros(4, dtype=np.int8)}


def fake_sources():
    return {"node_idx": np.array([0, 1, 3, 5, -1]),
            "tier": np.array([0, 0, 0, 0, -1], dtype=np.int8),
            "offset": np.array([0.0, 0.0, 0.0, 0.0, float("inf")]),
            "ids": np.array(["0000000001", "0000000002", "0000000003", "0000000004", "0000000005"]),
            "names": np.array(["Main A", "Main B", "Near orphan", "Far orphan", "Not snapped"]),
            "lonlat": np.array([[120.0, 10.0], [120.001, 10.0], [120.0011, 10.0],
                                [121.0, 11.0], [122.0, 12.0]])}


def test_analyze_identifies_near_gap_and_far_gap_without_changing_arrays():
    graph, source = fake_graph(), fake_sources()
    old_u, old_idx = graph["u"].copy(), source["node_idx"].copy()
    summary, gaps, towns, geo = diag.analyze_graph(graph, source)
    assert summary["main_component_towns"] == 2
    assert summary["smaller_components_with_towns"] == 2
    assert summary["unsnappable_count"] == 1
    assert summary["nearest_main_road_vertex_gap_under_100m"] == 1
    assert summary["nearest_main_road_vertex_gap_under_10m"] == 0
    assert gaps[0]["component"] == "C0002"
    assert 0.005 < gaps[0]["nearest_main_road_vertex_km"] < 0.03
    assert gaps[1]["component"] == "C0003"
    assert len(towns) == 3
    assert {x["name"] for x in towns} == {"Near orphan", "Far orphan", "Not snapped"}
    assert len(geo["features"]) == 2
    np.testing.assert_array_equal(graph["u"], old_u)
    np.testing.assert_array_equal(source["node_idx"], old_idx)


def test_read_only_run_writes_reports_and_preserves_checkpoint_hashes(tmp_path, monkeypatch):
    graph = fake_graph()
    sources = fake_sources()
    work = tmp_path / "work"
    work.mkdir()
    np.savez_compressed(work / "graph_full.npz", **graph)
    np.savez(work / "sources.npz", **sources,
             input_manifest=np.array(json.dumps({"fingerprint": "synthetic"})))
    before_graph = diag.Path(work / "graph_full.npz").read_bytes()
    before_sources = (work / "sources.npz").read_bytes()
    assert diag.run(workspace=tmp_path, verify=False, top=2) == 0
    output = tmp_path / "output" / "connectivity_diagnostics"
    for fname in ("component_gaps.csv", "affected_towns.csv", "component_gap_lines.geojson", "diagnostic_summary.json"):
        assert (output / fname).exists()
    report = json.loads((output / "diagnostic_summary.json").read_text())
    assert not report["provenance_checked"]
    assert report["source_fingerprint"] == "synthetic"
    assert (work / "graph_full.npz").read_bytes() == before_graph
    assert (work / "sources.npz").read_bytes() == before_sources


def test_cli_diagnose_dispatch_and_non_town_workspace(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(diag, "run", lambda **k: (captured.append(k) or 0))
    assert cli.main(["diagnose-connectivity", "--workspace", str(tmp_path), "--top", "4"]) == 0
    assert captured[-1]["workspace"] == tmp_path.resolve()
    assert captured[-1]["top"] == 4
    assert cli.main(["diagnose-connectivity", "--workspace", str(tmp_path),
                     "--boundary-level", "province"]) == 0
    assert captured[-1]["workspace"] == (tmp_path / "levels" / "province").resolve()


def test_invalid_graph_and_argument_rejected(tmp_path):
    graph, sources = fake_graph(), fake_sources()
    graph["u"] = np.array([999, 1, 3, 5])
    with pytest.raises(ValueError):
        diag.analyze_graph(graph, sources)
    with pytest.raises(ValueError, match="positive"):
        diag.run(workspace=tmp_path, top=0)
