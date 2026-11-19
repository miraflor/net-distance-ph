"""Regressions for v3 cache identity and concurrency safeguards."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from net_distance_ph import population_routing as route


def _minimal_core(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    for name in route._CORE_IDENTITY_FILES:
        np.save(core / name, np.array([1, 2, 3], dtype=np.int64))
    (core / "metadata.json").write_text(json.dumps({
        "core_builder": route.CORE_BUILDER,
        "core_sha256": route._core_content_sha256(core),
        "core_sha256_version": route._CORE_SHA256_VERSION,
    }))
    return core


@pytest.mark.parametrize("name", [
    "original_index_per_bar.npy", "offset_per_bar.npy",
    "snapped_node_lonlat.npy", "valid_barangay_mask.npy",
])
def test_v3_fingerprint_includes_all_site_and_verification_inputs(tmp_path, name):
    core = _minimal_core(tmp_path)
    assert route._verified_core_sha256(core)
    np.save(core / name, np.array([7, 8, 9], dtype=np.int64))
    with pytest.raises(ValueError, match="core content differs"):
        route._verified_core_sha256(core)


def test_old_five_file_hash_is_distinguishable_from_complete_hash(tmp_path):
    core = _minimal_core(tmp_path)
    previous = route._core_content_sha256(core, files=route._PREVIOUS_CORE_IDENTITY_FILES)
    current = route._core_content_sha256(core)
    assert previous != current
    info = json.loads((core / "metadata.json").read_text())
    info["core_sha256"] = previous
    info.pop("core_sha256_version")
    (core / "metadata.json").write_text(json.dumps(info))
    with pytest.raises(ValueError, match="Older barangay core digest"):
        route._verified_core_sha256(core)


@pytest.mark.parametrize("scope", ["sites", "routing", "run"])
def test_v3_cache_lock_excludes_concurrent_call_and_recovers(tmp_path, scope):
    with route._population_cache_lock(tmp_path, scope):
        with pytest.raises(RuntimeError, match="population-weighted"):
            with route._population_cache_lock(tmp_path, scope):
                pass
    with route._population_cache_lock(tmp_path, scope):
        pass


def test_run_lock_held_before_any_pipeline_io(tmp_path):
    args = SimpleNamespace(workspace=str(tmp_path))
    home = tmp_path / "variants" / "population_weighted"
    with route._population_cache_lock(home, "run"):
        with pytest.raises(RuntimeError, match="population-weighted run"):
            route.run(args)


@pytest.mark.parametrize("scope", ["sites", "routing"])
def test_direct_cache_functions_hold_their_scope_lock(tmp_path, scope):
    home = tmp_path / "home"
    with route._population_cache_lock(home, scope):
        with pytest.raises(RuntimeError, match=f"population-weighted {scope}"):
            if scope == "sites":
                route.build_town_sites(home, None, "some-key", [], {}, {"selection": {}})
            else:
                route.compute_routing(home, None, [], "some-key", workers=1)


def test_existing_v2_hashed_core_migrates_after_verification(tmp_path):
    # The core is built normally; then simulate the old five-file v2 digest.
    workspace = tmp_path / "workspace"
    work = workspace / "work"
    work.mkdir(parents=True)
    lonlat = np.array([[120., 14.], [120.01, 14.]])
    np.savez(work / "graph_full.npz", node_lonlat=lonlat,
             flags=np.array([[True, False, False], [True, False, False]]),
             u=np.array([0]), v=np.array([1]), w=np.array([1.]))
    np.savez(work / "sources.npz", input_manifest=np.array(json.dumps({"fingerprint": "toy"})))
    points = tmp_path / "points.csv"
    points.write_text("psgc_code,lon,lat\n0128010001,120.0,14.0\n0128010002,120.01,14.0\n")
    rows = [{"psgc_code": "0128010001", "town_psgc": "0128010000", "population": 15},
            {"psgc_code": "0128010002", "town_psgc": "0128010000", "population": 5}]
    pop_meta = {"data_file_sha256": "toy", "source": {"units_sha256": "toy"}}
    params = {"positions": lonlat, "max_snap_km": 25., "unsnappable_policy": "exclude"}
    home = workspace / "variants" / "population_weighted"
    core, key = route.points_core(workspace, home, rows, pop_meta, points, params)
    info_path = core / "metadata.json"
    info = json.loads(info_path.read_text())
    info["core_sha256"] = route._core_content_sha256(core,
                                                     files=route._PREVIOUS_CORE_IDENTITY_FILES)
    info.pop("core_sha256_version")
    info_path.write_text(json.dumps(info))
    again, same_key = route.points_core(workspace, home, rows, pop_meta, points, params)
    assert (again, same_key) == (core, key)
    info = json.loads(info_path.read_text())
    assert info["core_sha256_version"] == route._CORE_SHA256_VERSION
    assert info["core_sha256"] == route._core_content_sha256(core)
