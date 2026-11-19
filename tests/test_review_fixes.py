"""Regression tests for the fixes from the November 2026 code review."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.sparse.csgraph import dijkstra

from net_distance_ph import graphbuild as gb
from net_distance_ph import matrix as mx
from net_distance_ph import population_routing as route
from net_distance_ph.reduce import dedupe_edges
from net_distance_ph.shpio import Parts


def _parts(lines):
    xy = np.concatenate([np.asarray(line, float) for line in lines])
    offsets = np.cumsum([0] + [len(line) for line in lines]).astype(np.int64)
    return Parts(xy=xy, part_offset=offsets, part_record=np.arange(len(lines)), n_records=len(lines))


def _gangways(roads, ferries, rule):
    u, v, w, code, ll, flags = gb.build_graph([(roads, gb.ROAD), (ferries, gb.FERRY)],
                                              verbose=False, ferry_terminal_rule=rule)
    u, v, w, code = gb.connect_ferries(u, v, w, code, ll, flags, ferry_connect_km=5.0, verbose=False)
    m = code == gb.GANGWAY
    return {tuple(ll[a]) for a in u[m]}


# Port A -> split point S (at sea) -> port B, stored as two ferry ways.
# Island C has a road ending 2.2 km from S; no ferry stops at C.
SPLIT_ROADS = _parts([
    [(120.00, 10.00), (120.01, 10.00)],
    [(120.40, 10.00), (120.41, 10.00)],
    [(120.20, 10.05), (120.20, 10.02)],
])
SPLIT_FERRIES = _parts([
    [(120.01, 10.00), (120.10, 10.00), (120.20, 10.00)],
    [(120.20, 10.00), (120.30, 10.00), (120.40, 10.00)],
])


def test_part_endpoint_rule_keeps_historical_gangway_at_split_point():
    assert (120.20, 10.00) in _gangways(SPLIT_ROADS, SPLIT_FERRIES, "part_endpoint")


def test_ferry_degree_1_rule_has_no_gangway_at_split_point():
    assert (120.20, 10.00) not in _gangways(SPLIT_ROADS, SPLIT_FERRIES, "ferry_degree_1")


def test_ferry_report_counts_actual_ambiguous_gangways(capsys):
    u,v,w,c,ll,flags=gb.build_graph([(SPLIT_ROADS,gb.ROAD),(SPLIT_FERRIES,gb.FERRY)],
                                    verbose=False, ferry_terminal_rule="part_endpoint")
    gb.connect_ferries(u,v,w,c,ll,flags,ferry_connect_km=5.,verbose=True)
    assert "ambiguous gangways at off-road degree-2 ferry endpoints: 1" in capsys.readouterr().out


def test_ferry_degree_1_rule_also_drops_a_port_shared_by_two_lines():
    """Documented cost of the stricter rule: an offshore port where exactly two
    ferry lines end looks the same as a split point."""
    roads = _parts([[(121.00, 11.00), (121.00, 11.02)]])           # road ends 2.2 km from P
    ferries = _parts([[(121.00, 10.98), (121.30, 10.98)],          # line 1 ends at port P
                      [(121.00, 10.98), (120.70, 10.98)]])         # line 2 ends at port P
    port = (121.00, 10.98)
    assert port in _gangways(roads, ferries, "part_endpoint")
    assert port not in _gangways(roads, ferries, "ferry_degree_1")


def test_unknown_ferry_terminal_rule_is_rejected():
    with pytest.raises(ValueError, match="ferry_terminal_rule"):
        gb.build_graph([(SPLIT_ROADS, gb.ROAD)], verbose=False, ferry_terminal_rule="nearest")


def test_stage1_graph_without_new_setting_stays_current_under_historical_default():
    import run
    current = run._stage1_config()
    recorded = {k: v for k, v in current.items() if k != "FERRY_TERMINAL_RULE"}
    assert current["FERRY_TERMINAL_RULE"] == "part_endpoint"
    assert run._stage1_changes(recorded, current) == []
    assert run._stage1_changes(recorded, {**current, "FERRY_TERMINAL_RULE": "ferry_degree_1"}) == [
        "FERRY_TERMINAL_RULE"]


def test_directed_true_matches_directed_false_on_symmetric_csr():
    rng = np.random.default_rng(3)
    n = 300
    u = rng.integers(0, n, 1200)
    v = rng.integers(0, n, 1200)
    u, v, w = dedupe_edges(u, v, rng.uniform(0.1, 5.0, u.size), n)
    G = mx.build_csr(u, v, w, n)
    sources = [0, 17, 299]
    assert np.array_equal(dijkstra(G, directed=True, indices=sources),
                          dijkstra(G, directed=False, indices=sources))
    assert np.array_equal(dijkstra(G, directed=True, indices=sources, limit=3.0),
                          dijkstra(G, directed=False, indices=sources, limit=3.0))


def _toy_workspace(tmp_path):
    """Two road components: a populated line and an empty loop with a junction."""
    workspace = tmp_path / "workspace"
    work = workspace / "work"
    work.mkdir(parents=True)
    lonlat = np.array([[120.00, 14.0], [120.01, 14.0], [120.02, 14.0],            # populated line
                       [121.00, 15.0], [121.01, 15.0], [121.01, 15.01],           # empty loop
                       [121.00, 15.01]], dtype=float)
    u = np.array([0, 1, 3, 4, 5, 6, 4])
    v = np.array([1, 2, 4, 5, 6, 3, 6])                                           # node 4 and 6: degree 3
    w = np.ones(u.size)
    flags = np.zeros((lonlat.shape[0], 3), dtype=bool)
    flags[:, 0] = True
    np.savez(work / "graph_full.npz", node_lonlat=lonlat, flags=flags, u=u, v=v, w=w,
             code=np.zeros(u.size, dtype=np.int8))
    np.savez(work / "sources.npz", input_manifest=np.array(json.dumps({"fingerprint": "toy"})))
    points = tmp_path / "points.csv"
    points.write_text("psgc_code,lon,lat\n0128010001,120.00,14.0\n0128010002,120.02,14.0\n")
    rows = [{"psgc_code": "0128010001", "town_psgc": "0128010000", "population": 15, "barangay_name": "A"},
            {"psgc_code": "0128010002", "town_psgc": "0128010000", "population": 5, "barangay_name": "B"}]
    meta = {"data_file_sha256": "populationhash", "source": {"units_sha256": "unitshash"}}
    params = {"positions": np.array([[120.00, 14.0], [120.02, 14.0]]),
              "max_snap_km": 25.0, "unsnappable_policy": "exclude"}
    return workspace, workspace / "variants" / "population_weighted", rows, meta, points, params


def test_points_core_drops_components_without_origins(tmp_path):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, key = route.points_core(workspace, home, rows, meta, points, params)
    info = json.loads((core / "metadata.json").read_text())
    assert info["core_builder"] == route.CORE_BUILDER
    assert info["core_sha256"] == route._core_content_sha256(core)
    assert info["n_core_vertices"] == 2            # only the two origin nodes; the loop is gone
    assert info["n_core_edges"] == 1
    G = mx.load_core(core, mmap=False)
    idx = np.load(core / "core_index_per_bar.npy")
    assert dijkstra(G, directed=True, indices=int(idx[0]))[idx[1]] == pytest.approx(2.0)
    assert not list(home.joinpath("cores").glob(".*partial*"))


def test_points_core_recovers_from_an_unfinished_build(tmp_path):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, key = route.points_core(workspace, home, rows, meta, points, params)
    # Simulate the folder an interrupted build of earlier versions left behind:
    (core / "metadata.json").unlink()
    again, again_key = route.points_core(workspace, home, rows, meta, points, params)
    assert again == core and again_key == key
    assert (core / "metadata.json").is_file()
    assert len(list(home.joinpath("cores").glob(f"{core.name}.incomplete-*"))) == 1


def test_points_core_failure_does_not_block_the_next_run(tmp_path, monkeypatch):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(route.mx, "build_csr", interrupted)
    with pytest.raises(KeyboardInterrupt):
        route.points_core(workspace, home, rows, meta, points, params)
    monkeypatch.undo()
    core, _ = route.points_core(workspace, home, rows, meta, points, params)
    assert (core / "metadata.json").is_file()


def test_points_core_still_refuses_a_cache_key_collision(tmp_path):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, _ = route.points_core(workspace, home, rows, meta, points, params)
    info = json.loads((core / "metadata.json").read_text())
    info["cache_key"] = "f" * 64
    (core / "metadata.json").write_text(json.dumps(info))
    with pytest.raises(RuntimeError, match="collision"):
        route.points_core(workspace, home, rows, meta, points, params)


def test_town_sites_from_another_core_builder_are_recomputed(tmp_path):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, key = route.points_core(workspace, home, rows, meta, points, params)
    roster = {"0128010000": {"geographic_level": "Mun", "name": "Toy"}}
    selection = {"median_selection": "balanced", "mode": "balanced",
                 "max_mean_access_km": None, "max_sites_per_town": 1}
    sites = route.build_town_sites(home, core, key, rows, roster, {"selection": selection})
    cache = next((home / "town_sites" / key).glob("*/0128010000.json"))
    stored = json.loads(cache.read_text())
    assert stored["core_builder"] == route.CORE_BUILDER
    assert stored["core_sha256"] == route._core_content_sha256(core)
    # A cache written for a core numbered by another builder must not be reused.
    stored["core_builder"] = route.LEGACY_CORE_BUILDER
    stored["sites"][0]["core_node"] = 999
    cache.write_text(json.dumps(stored))
    again = route.build_town_sites(home, core, key, rows, roster, {"selection": selection})
    assert again == sites
    audit = next((home / "town_sites" / key).glob("*/selection_audit.csv")).read_text().splitlines()
    assert audit[0].endswith(",reached_site_cap")
    assert audit[1].endswith(",1")                 # 2 demand nodes; cap reached, not proven binding


def test_core_lock_is_released_after_failure(tmp_path):
    core_root = tmp_path / "cores"
    with pytest.raises(KeyboardInterrupt):
        with route._population_core_lock(core_root):
            raise KeyboardInterrupt
    with route._population_core_lock(core_root):
        with pytest.raises(RuntimeError, match="workspace lock"):
            with route._population_core_lock(core_root):
                pass
    with route._population_core_lock(core_root):
        pass


def test_site_cache_rejects_core_digest_mismatch(tmp_path):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, key = route.points_core(workspace, home, rows, meta, points, params)
    roster = {"0128010000": {"geographic_level": "Mun", "name": "Toy"}}
    selection = {"median_selection": "balanced", "mode": "balanced",
                 "max_mean_access_km": None, "max_sites_per_town": 1}
    sites = route.build_town_sites(home, core, key, rows, roster, {"selection": selection})
    cache = next((home / "town_sites" / key).glob("*/0128010000.json"))
    stored = json.loads(cache.read_text())
    stored.pop("core_sha256")
    stored["sites"][0]["core_node"] = 999
    cache.write_text(json.dumps(stored))
    assert route.build_town_sites(home, core, key, rows, roster, {"selection": selection}) == sites
    assert json.loads(cache.read_text())["core_sha256"] == route._core_content_sha256(core)


def test_core_digest_detects_tampering(tmp_path):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, key = route.points_core(workspace, home, rows, meta, points, params)
    data = core / "core_data.npy"
    arr = np.load(data)
    arr[0] += .5
    np.save(data, arr)
    with pytest.raises(ValueError, match="core content differs"):
        route._verified_core_sha256(core)


def test_existing_v2_core_is_migrated_without_rebuilding_graph(tmp_path):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, key = route.points_core(workspace, home, rows, meta, points, params)
    info_path = core / "metadata.json"
    stored = json.loads(info_path.read_text())
    stored.pop("core_sha256")
    info_path.write_text(json.dumps(stored))
    new_core, new_key = route.points_core(workspace, home, rows, meta, points, params)
    assert new_core == core and new_key == key
    assert json.loads(info_path.read_text())["core_sha256"] == route._core_content_sha256(core)


def test_v2_routing_cache_key_tracks_core_content(tmp_path):
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, key = route.points_core(workspace, home, rows, meta, points, params)
    idx = np.load(core / "core_index_per_bar.npy")
    sites = [{"town_psgc": "0128010000", "core_node": int(idx[0]),
              "population": 20}]
    _, _, _, first = route.compute_routing(home, core, sites, key, workers=1,
                                          all_towns=["0128010000"])
    data_path = core / "core_data.npy"
    arr = np.load(data_path)
    arr[0] += 0.5
    np.save(data_path, arr)
    info_path = core / "metadata.json"
    info = json.loads(info_path.read_text())
    info["core_sha256"] = route._core_content_sha256(core)
    info_path.write_text(json.dumps(info))
    _, _, _, second = route.compute_routing(home, core, sites, key, workers=1,
                                           all_towns=["0128010000"])
    assert first != second


def test_checkpoint_resume_rejects_partial_legacy_state():
    import run
    sig = dict(graph_size=123, graph_mtime_ns=4, sources_size=55, sources_mtime_ns=6)
    base = {**sig, "u": np.array([0]), "v": np.array([1]),
            "w": np.array([1.]), "src": np.array([0]), "n_nodes": 2,
            "checkpoint_version": 1}
    class Record(dict):
        @property
        def files(self):
            return list(self)
    assert run._checkpoint_matches(Record(base), sig)
    partial = Record({**base, "next_phase": 1, "next_pass": 2,
                      "completed_passes": 2, "best_edges": 1, "alive": np.array([True, True])})
    assert not run._checkpoint_matches(partial, sig)
    ready = Record({**base, "next_phase": 0, "next_pass": 0,
                    "completed_passes": 0, "best_edges": -1, "alive": np.array([], dtype=bool)})
    assert run._checkpoint_matches(ready, sig)
    assert run._checkpoint_matches(Record({**base, "checkpoint_version": 2}), sig)


def test_convert_psgc_compares_gzip_content_not_bytes(tmp_path):
    import gzip
    import io
    from scripts.convert_psgc import same_content

    payload = b'{"psgc_code":"0100000000"}\n' * 50

    def compress(level):
        data = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=data, mtime=0, compresslevel=level) as gz:
            gz.write(payload)
        return data.getvalue()

    stored = tmp_path / "records.jsonl.gz"
    stored.write_bytes(compress(1))
    assert compress(1) != compress(9)              # different bytes, same content
    assert same_content(stored, compress(9))
    assert not same_content(stored, gzip.compress(payload + b"x"))


def test_single_process_routing_releases_the_core_memory_map(tmp_path):
    """Windows cannot rewrite or delete a memory-mapped file, so single-process
    routing must not keep the core mapped after it returns."""
    workspace, home, rows, meta, points, params = _toy_workspace(tmp_path)
    core, key = route.points_core(workspace, home, rows, meta, points, params)
    idx = np.load(core / "core_index_per_bar.npy")
    sites = [{"town_psgc": "0128010000", "core_node": int(idx[0]), "population": 20}]
    route.compute_routing(home, core, sites, key, workers=1, all_towns=["0128010000"])
    assert route._ROUTE_G is None
    maps = Path("/proc/self/maps")
    if maps.is_file():  # Linux only: check that the operating system unmapped the file
        assert str((core / "core_data.npy").resolve()) not in maps.read_text()
