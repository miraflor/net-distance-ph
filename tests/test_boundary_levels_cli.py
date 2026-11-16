"""Offline contract tests: no national downloads, no real routing claims."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from net_distance_ph import boundary_levels as levels, boundaries, cli, workspace


def test_level_inventory_and_pinned_assets():
    assert set(levels.LEVELS) == {"town", "barangay", "city", "municipality", "huc", "province", "region"}
    assert levels.COUNT["town"] == 1642
    assert levels.COUNT["huc"] == 33
    assert levels.COUNT["barangay"] == 42010
    assert levels.ASSETS["barangay"][0].endswith("_complete.shp.zip")
    assert all(len(h) == 64 for _, h in levels.ASSETS.values())


def test_town_is_default_and_cli_help_shows_all_levels():
    parser = cli.build_parser()
    assert parser.parse_args(["run", "--until", "connectivity"]).boundary_level == "town"
    for level in levels.LEVELS:
        assert parser.parse_args(["run", "--until", "connectivity", "--boundary-level", level]).boundary_level == level
        assert parser.parse_args(["boundaries", "--download", "--boundary-level", level]).boundary_level == level


def test_non_town_plan_is_isolated_and_sets_env(tmp_path, monkeypatch):
    invocations = []
    def fake_run(argv, **kw):
        invocations.append((argv, kw))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["run", "--until", "connectivity", "--boundary-level", "huc",
                     "--workspace", str(tmp_path)]) == 0
    assert [x[0][3] for x in invocations] == ["prepare", "graph", "connectivity"]
    assert all(x[1]["env"]["NET_DISTANCE_PH_BOUNDARY_LEVEL"] == "huc" for x in invocations)
    assert all(x[0][x[0].index("--workspace") + 1] == str(tmp_path / "levels" / "huc")
               for x in invocations)


def test_non_town_rejects_unscoped_explicit_data(tmp_path):
    assert cli.main(["prepare", "--boundary-level", "city", "--data", str(tmp_path)]) == 2


def test_auto_prepare_uses_boundaries_adapter(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("NET_DISTANCE_PH_BOUNDARY_LEVEL", "province")
    monkeypatch.setattr(boundaries, "run", lambda **kw: (seen.append(kw) or 0))
    workspace._build_boundaries(tmp_path / "data")
    assert seen == [{"workspace": tmp_path, "download": True, "publish": True, "level": "province"}]


def _dummy_shapes(n, code_col, name_col, kind, *, expected_codes=None):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box
    codes = expected_codes or [f"{i+1:010d}" for i in range(n)]
    geometry = [box(400000 + (i % 300) * 20, 1500000 + (i // 300) * 20,
                    400010 + (i % 300) * 20, 1500010 + (i // 300) * 20) for i in range(n)]
    return gpd.GeoDataFrame({code_col: [int(s) for s in codes], name_col: [f"Area {i}" for i in range(n)],
                             "geo_level": [kind] * n}, geometry=geometry, crs="EPSG:32651")


@pytest.mark.parametrize("level", ["huc", "province", "region"])
def test_non_town_validation_and_explicit_publication(tmp_path, monkeypatch, level):
    n = levels.COUNT[level]
    code_col, name_col = levels.ATTRS[level]
    frame = _dummy_shapes(n, code_col, name_col, levels.GEO_TYPES[level])
    codes = [f"{i+1:010d}" for i in range(n)]
    official = [dict(psgc_code=c, name=f"Area {i}", province="") for i, c in enumerate(codes)]
    workbook = tmp_path / "original.xlsx"
    workbook.write_bytes(b"fake-synthetic-workbook")
    monkeypatch.setattr(levels, "_official", lambda _: (official, workbook))
    monkeypatch.setattr(boundaries, "_read_shapefile", lambda _: frame)
    zip_path = tmp_path / "dummy.zip"
    zip_path.write_bytes(b"synthetic-not-national")
    valid, roster, report = levels.validate_level(zip_path, level, expected_sha=boundaries.sha256(zip_path))
    assert len(valid) == n and len(roster) == n and report["boundary_level"] == level
    assert not (tmp_path / "data").exists()
    levels._save_release(valid, roster, report, tmp_path, replace=False)
    pointer = json.loads((tmp_path / "data" / "current_boundaries.json").read_text(encoding="utf-8"))
    assert pointer["boundary_level"] == level
    assert (tmp_path / "data" / pointer["roster"]).is_file()
    assert (tmp_path / "data" / pointer["centroids"]).is_file()
    assert workspace._selected(tmp_path / "data", "current_boundaries.json", {"centroids": "muncity_centroids.shp"})
    with pytest.raises(FileExistsError):
        levels._save_release(valid, roster, report, tmp_path, replace=False)


def test_release_level_roster_missing_is_rejected(tmp_path):
    data = tmp_path / "data"
    d = data / "boundary_releases" / "test"
    d.mkdir(parents=True)
    for ext in ("shp", "shx", "dbf"):
        (d / f"muncity_centroids.{ext}").write_bytes(b"fixture")
    (data / "current_boundaries.json").write_text(json.dumps({
        "boundary_level": "region", "centroids": "boundary_releases/test/muncity_centroids.shp",
        "roster": "../../outside.json"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="roster"):
        workspace._selected(data, "current_boundaries.json", {"centroids": "muncity_centroids.shp"})


def test_connectivity_large_component_pair_summary_no_quadratic_heap():
    import numpy as np
    from net_distance_ph.connectivity import audit
    n = 3000
    codes = [f"{i:010d}" for i in range(1,n+1)]
    roster = [dict(psgc_code=code,name=code,province="") for code in codes]
    report, _, _, barriers = audit(roster, codes, codes,
        np.arange(n), np.zeros(n,dtype=np.int8), np.zeros(n), np.zeros((n,2)),
        np.array([],dtype=np.int64),np.array([],dtype=np.int64),
        np.array([],dtype=np.int8),np.zeros((n,3),dtype=np.bool_))
    assert report["snapped_but_disconnected_pairs"] == n*(n-1)//2
    assert len(barriers) == 100


def test_pinned_psa_workbook_has_33_hucs():
    # The source workbook, not third-party polygon attributes, defines HUC.
    rows, workbook = levels._official("huc")
    assert workbook.is_file()
    assert len(rows) == 33
    assert len({r["psgc_code"] for r in rows}) == 33
