"""v54: independent province-equivalent unit census conservation and CLI regression."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from net_distance_ph.province_equivalent import build_province_equivalents


def _roster():
    # One ordinary provincial town, one province-less HUC/ICC-style city,
    # Pateros-style nonprovincial municipality and an SGA municipality.
    return {
        "0100000000": {"level": "region", "name": "Region One"},
        "1300000000": {"level": "region", "name": "NCR"},
        "1900000000": {"level": "region", "name": "BARMM"},
        "0128000000": {"level": "province", "name": "Province A"},
        "0128010000": {"level": "municipality", "name": "Town A", "province_psgc": "0128000000"},
        "1380600000": {"level": "city", "name": "City B", "province_psgc": ""},
        "1376060000": {"level": "municipality", "name": "Pateros", "province_psgc": ""},
        "1900101000": {"level": "municipality", "name": "Special Area Town", "province_psgc": ""},
    }


def test_exhaustive_without_fabricating_provincial_parents():
    towns = ["1380600000", "0128010000", "1900101000", "1376060000"]
    units, mapping, meta = build_province_equivalents(_roster(), towns)
    assert len(units) == 4
    assert meta["n_towns"] == 4
    assert meta["towns_without_parent"] == 3
    assert meta["unit_types"] == {
        "nonprovincial_city": 1, "nonprovincial_municipality": 2,
        "province": 1,
    }
    by_code = {u["code"]: u for u in units}
    assert by_code["1376060000"]["unit_type"] == "nonprovincial_municipality"
    assert by_code["1380600000"]["unit_type"] == "nonprovincial_city"
    assert by_code["0128000000"]["unit_type"] == "province"
    for town, index in zip(towns, mapping):
        assert units[index]["code"] == ("0128000000" if town == "0128010000" else town)


def test_populations_preserved_exactly():
    towns = sorted(k for k, r in _roster().items() if r["level"] in {"city", "municipality"})
    units, mapping, _ = build_province_equivalents(_roster(), towns)
    population = np.asarray([10, 200, 500, 71], dtype=np.float64)
    folded = np.bincount(mapping, weights=population, minlength=len(units))
    assert folded.sum() == population.sum()
    assert not np.any(mapping < 0)


def test_shared_province_sums_both_towns():
    roster = _roster()
    roster["0128020000"] = {"level": "city", "name": "City A", "province_psgc": "0128000000"}
    towns = sorted(k for k, r in roster.items() if r["level"] in {"city", "municipality"})
    units, mapping, _ = build_province_equivalents(roster, towns)
    province_index = next(i for i, u in enumerate(units) if u["code"] == "0128000000")
    assert list(mapping).count(province_index) == 2
    assert len(units) == len(towns) - 1


def test_explicit_crosswalk_is_not_implicit():
    roster = _roster()
    towns = sorted(k for k, r in roster.items() if r["level"] in {"city", "municipality"})
    units, mapping, meta = build_province_equivalents(roster, towns, {"1380600000": "0128000000"})
    assert meta["explicit_crosswalk_assignments"] == 1
    assert meta["towns_without_parent"] == 3
    assert not any(u["code"] == "1380600000" for u in units)


@pytest.mark.parametrize("crosswalk", [
    {"1380600000": "9999999999"},
    {"0128010000": "9999999999"},
    {"9999999999": "0128000000"},
])
def test_invalid_crosswalk_refused(crosswalk):
    roster = _roster()
    towns = sorted(k for k, r in roster.items() if r["level"] in {"city", "municipality"})
    with pytest.raises(ValueError):
        build_province_equivalents(roster, towns, crosswalk)


def test_unknown_town_and_duplicate_town_refused():
    r = _roster()
    towns = sorted(k for k, v in r.items() if v["level"] in {"city", "municipality"})
    with pytest.raises(ValueError, match="exactly once"):
        build_province_equivalents(r, towns + [towns[0]])
    with pytest.raises(ValueError, match="exactly once"):
        build_province_equivalents(r, towns[:-1])


def test_missing_published_parent_refused():
    r = _roster()
    r["0128010000"]["province_psgc"] = "9999999999"
    towns = sorted(k for k, v in r.items() if v["level"] in {"city", "municipality"})
    with pytest.raises(ValueError, match="unrecognized province"):
        build_province_equivalents(r, towns)


def test_cli_defaults_and_explicit_outputs():
    from net_distance_ph.population_weighted import parse_args
    default = parse_args([])
    assert default.level == "all"
    assert default.median_selection == "balanced"
    assert parse_args(["--level", "province"]).level == "province"
    assert parse_args(["--level", "province-equivalent"]).level == "province-equivalent"
    assert parse_args(["--level", "all"]).level == "all"
    assert parse_args(["--median-selection", "one"]).median_selection == "one"


def test_province_equivalent_matrix_conserves_mass_and_publishes_type(tmp_path):
    from net_distance_ph.population_routing import _save_level
    from net_distance_ph.population_weighted import combine_group_matrices
    r = _roster()
    towns = sorted(k for k, v in r.items() if v["level"] in {"city", "municipality"})
    units, mapping, _ = build_province_equivalents(r, towns)
    pops = np.array([100, 200, 80, 90.], dtype=float)
    # Dense modeled OD pairs, with zero self distance; aggregate without
    # recomputing routes and retain the ordinary input-population denominator.
    distances = np.array([[0., 1, 2, 3], [1, 0., 4, 5], [2, 4, 0., 6], [3, 5, 6, 0.]])
    numerator = distances * pops[:, None] * pops[None, :]
    denominator = pops[:, None] * pops[None, :]
    sites = [{"town_psgc": t, "population": int(p), "mean_access_km": 0.}
             for t, p in zip(towns, pops)]
    root = tmp_path / "variants" / "population_weighted"
    extra = {"population_total": float(pops.sum()), "source": {"units_sha256": "a"}}
    dest = _save_level(root, "province_equivalent", units, mapping, numerator, denominator,
                       pops, sites, extra, route_hash="a"*64, core_key="b"*64,
                       full_town_pop=pops, town_codes=towns)
    assert dest.name == "province_equivalent"
    table = np.load(dest / "distance_matrix_km.npy")
    assert table.shape == (len(units),len(units))
    assert np.allclose(np.load(dest / "reachable_pair_probability.npy"), 1.)
    assert np.load(dest / "population_per_unit.npy").sum() == pops.sum()
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["national_population_exhaustive"] is True
    assert manifest["excluded_population_by_level"] == 0
    assert "unit_type" in (dest / "units.csv").read_text(encoding="utf-8").splitlines()[0]


def test_geographic_audit_resolver_accepts_default_town_only_run(tmp_path):
    from net_distance_ph.geographic_audit import _resolve_run
    ws = tmp_path / "ws"
    base = ws / "variants" / "population_weighted" / "output"
    run_hash = "f" * 64
    run = base / run_hash[:16]
    (run / "town").mkdir(parents=True)
    (run / "town" / "manifest.json").write_text(
        json.dumps({"routing_hash":run_hash,"core_cache_key":"c"*64,"level":"town"}),
        encoding="utf-8")
    (base / "latest_run.json").write_text(
        json.dumps({"routing_hash":run_hash,"directory":str(run),"levels":["town"]}),
        encoding="utf-8")
    resolved, town_manifest, upper, key = _resolve_run(ws)
    assert resolved == run.resolve()
    assert upper is None
    assert key == run_hash


def test_geographic_audit_resolver_prefers_new_tier_over_legacy(tmp_path):
    from net_distance_ph.geographic_audit import _resolve_run
    ws = tmp_path / "ws"
    base = ws / "variants" / "population_weighted" / "output"
    run_hash = "f" * 64
    run = base / run_hash[:16]
    for level in ("town", "province", "province_equivalent"):
        (run / level).mkdir(parents=True)
        (run / level / "manifest.json").write_text(
            json.dumps({"routing_hash":run_hash,"core_cache_key":"c"*64,"level":level}),
            encoding="utf-8")
    (base / "latest_run.json").write_text(
        json.dumps({"routing_hash":run_hash,"directory":str(run),"levels":["town","province_equivalent"]}),
        encoding="utf-8")
    assert _resolve_run(ws)[2]["level"] == "province_equivalent"
