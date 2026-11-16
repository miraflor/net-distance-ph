"""Synthetic offline tests for OSM node-ID gap inspection, no national download."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from net_distance_ph import osm_gap_inspection as inspect
from net_distance_ph import cli


def gap():
    return {"component": "C0004", "town_names": "Demo", "a": (120.0, 10.0),
            "b": (120.0001, 10.0), "km": 0.011}


def way(num, road, nodes, **kw):
    return {"way_id": num, "highway": road, "nodes": nodes, "tags": kw, "near_gap_m": 0.0}


def roads(access=""):
    return [
        way(11, "tertiary", [(1, 120, 10), (2, 120.00002, 10)]),
        way(12, "primary", [(3, 120.0001, 10), (4, 120.00012, 10)]),
        way(13, "unclassified", [(2, 120.00002, 10), (3, 120.0001, 10)], access=access),
    ]


def test_missing_minor_road_connects_exact_osm_node_ids():
    result, rows, geo = inspect.examine([gap()], {"C0004": roads()}, {"primary", "tertiary"}, 1000)
    r = result[0]
    assert r["baseline_local_path"] is False
    assert r["expanded_ordinary_local_path"] is True
    assert r["verdict"] == "EXPANDED_ORDINARY_ROADS_RECOVER_LOCAL_PATH"
    assert r["witness_added_classes"] == "unclassified"
    assert "13" in r["witness_way_ids"]
    assert len(rows) == 3
    assert len(geo["features"]) == 2


def test_geometrically_close_but_different_osm_node_ids_does_not_connect():
    w = roads()
    w[2] = way(13, "unclassified", [(22, 120.00002, 10), (33, 120.0001, 10)])
    r, _, _ = inspect.examine([gap()], {"C0004": w}, {"primary", "tertiary"}, 1000)
    assert r[0]["expanded_ordinary_local_path"] is False
    assert r[0]["verdict"] == "NO_LOCAL_OSM_NODE_PATH_IN_TESTED_RADIUS"


def test_restricted_way_cannot_produce_expanded_path():
    r, wayrows, _ = inspect.examine([gap()], {"C0004": roads(access="private")}, {"primary", "tertiary"}, 1000)
    assert r[0]["expanded_ordinary_local_path"] is False
    assert any(x["scenario"] == "restricted_or_area" for x in wayrows)


def test_service_road_sensitivity_is_separate():
    w = roads()
    w[2]["highway"] = "service"
    r, _, _ = inspect.examine([gap()], {"C0004": w}, {"primary", "tertiary"}, 1000)
    assert r[0]["expanded_ordinary_local_path"] is False
    assert r[0]["expanded_plus_service_local_path"] is True
    assert r[0]["verdict"] == "SERVICE_ROADS_RECOVER_LOCAL_PATH_ACCESS_REVIEW"


def test_road_crossing_box_without_way_node_inside_is_seen():
    p = gap()
    long_way = [(1, 119.999, 10), (2, 120.001, 10)]
    assert inspect.select_way_for_gap(long_way, p, 100) == 0.0
    assert inspect.select_way_for_gap([(1, 121, 11), (2, 122, 12)], p, 100) is None


def test_out_of_window_osm_path_cannot_create_false_positive():
    w = roads()
    w[2] = way(13, "unclassified", [(2, 120.00002, 10), (99, 120.05, 10), (3, 120.0001, 10)])
    r, _, _ = inspect.examine([gap()], {"C0004": w}, {"primary", "tertiary"}, 1000)
    assert r[0]["expanded_ordinary_local_path"] is False


def test_cli_dispatch(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(inspect, "run", lambda **kw: (called.append(kw) or 0))
    assert cli.main(["inspect-osm-gaps", "--workspace", str(tmp_path), "--top", "7", "--radius-m", "1500"]) == 0
    assert called[-1]["workspace"] == tmp_path.resolve()
    assert called[-1]["top"] == 7 and called[-1]["radius_m"] == 1500
    assert cli.main(["inspect-osm-gaps", "--workspace", str(tmp_path), "--boundary-level", "province"]) == 0
    assert called[-1]["workspace"] == (tmp_path / "levels" / "province").resolve()


def test_read_only_run_uses_cached_pbf_and_writes_all_reports(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "sources").mkdir()
    (tmp_path / "work").mkdir()
    diagnostic = tmp_path / "output" / "connectivity_diagnostics"
    diagnostic.mkdir(parents=True)
    pbf = data / "sources" / "philippines-latest.osm.pbf"
    pbf.write_bytes(b"fake synthetic PBF")
    sha = hashlib.sha256(pbf.read_bytes()).hexdigest()
    release = data / "transport_releases" / "test"
    release.mkdir(parents=True)
    (release / "transport_provenance.json").write_text(json.dumps({"source": str(pbf), "source_sha256": sha, "road_filter": ["primary", "tertiary"]}))
    (data / "current_transport.json").write_text(json.dumps({"provenance": "transport_releases/test/transport_provenance.json", "source_sha256": sha}))
    np.savez(tmp_path / "work" / "sources.npz", input_manifest=json.dumps({"fingerprint": "synthetic"}))
    (tmp_path / "work" / "graph_full.npz").write_bytes(b"do not modify")
    (diagnostic / "diagnostic_summary.json").write_text(json.dumps({
        "status": "DIAGNOSTIC_ONLY_NO_GRAPH_EDITS", "provenance_checked": True,
        "graph_path": str((tmp_path / "work" / "graph_full.npz").resolve()), "source_fingerprint": "synthetic"}))
    with (diagnostic / "component_gaps.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["component", "town_names", "nearest_main_road_vertex_km", "from_lon", "from_lat", "to_lon", "to_lat"])
        writer.writeheader()
        writer.writerow({"component": "C0004", "town_names": "Demo", "nearest_main_road_vertex_km": "0.011",
                         "from_lon": "120.0", "from_lat": "10.0", "to_lon": "120.0001", "to_lat": "10.0"})
    monkeypatch.setattr("net_distance_ph.connectivity_diagnostics._verify_saved_graph", lambda _: None)
    monkeypatch.setattr(inspect, "extract_nearby_ways", lambda *a: ({"C0004": roads()}, {"osm_ways_scanned": 5}))
    before = ((tmp_path / "work" / "graph_full.npz").read_bytes(), pbf.read_bytes())
    assert inspect.run(workspace=tmp_path) == 0
    output = tmp_path / "output" / "osm_gap_inspection"
    for fname in ("gap_findings.csv", "nearby_osm_ways.csv", "local_paths.geojson", "inspection_summary.json"):
        assert (output / fname).is_file()
    summary = json.loads((output / "inspection_summary.json").read_text())
    assert summary["source_pbf_sha256"] == sha
    assert summary["verdict_counts"]["EXPANDED_ORDINARY_ROADS_RECOVER_LOCAL_PATH"] == 1
    assert ((tmp_path / "work" / "graph_full.npz").read_bytes(), pbf.read_bytes()) == before
    pbf.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="Cached PBF SHA-256"):
        inspect.run(workspace=tmp_path)


def test_py_osmium_handler_keeps_only_nearby_original_osm_way_ids(tmp_path, monkeypatch):
    """Mock the PBF stream without network/PBF bytes; test handler contracts."""
    import sys
    from types import SimpleNamespace

    class Tag:
        def __init__(self, k, v): self.k, self.v = k, v

    class Node:
        def __init__(self, ref, lon, lat):
            self.ref = ref
            self.location = SimpleNamespace(lon=lon, lat=lat)

    class Way:
        def __init__(self, wid, tags, nodes):
            self.id = wid
            self.tags = [Tag(k, v) for k, v in tags.items()]
            self.nodes = [Node(*n) for n in nodes]

    samples = [
        Way(55, {"highway": "unclassified", "bridge": "yes"}, [(1, 120.0, 10.0), (2, 120.00002, 10.0)]),
        Way(66, {"highway": "tertiary"}, [(3, 121.0, 11.0), (4, 121.01, 11.0)]),
        Way(77, {"amenity": "school"}, [(5, 120, 10), (6, 120.001, 10)]),
    ]

    class Handler:
        def apply_file(self, filename, *, locations, idx):
            assert locations is True and idx == "flex_mem"
            for record in samples:
                self.way(record)

    monkeypatch.setitem(sys.modules, "osmium", SimpleNamespace(SimpleHandler=Handler))
    records, counts = inspect.extract_nearby_ways(tmp_path / "cached.osm.pbf", [gap()], 1000)
    assert counts["osm_ways_scanned"] == 3
    assert counts["highway_or_ferry_ways_seen"] == 2
    assert [w["way_id"] for w in records["C0004"]] == [55]
    assert records["C0004"][0]["tags"]["bridge"] == "yes"
