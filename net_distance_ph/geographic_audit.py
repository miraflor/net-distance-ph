"""Read-only-in-model geographic audit of an existing population-weighted run.

No routing, selection, source preparation or graph building is performed.
Writes *audit reports only* beneath the existing routing run output.

Two different connectivity concepts are kept separate:
- official locality centroids on the ORIGINAL stage-1 road/ferry graph;
- population representatives on the barangay-preserving core, for which
  reachable_pair_probability records weighted population-pair reachability.

A town with sites in multiple components is NOT assigned one component ID.
All distances in the model are undirected; ferry lines are not evidence of
an operating real-world ferry service.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import argparse
import csv
import hashlib
import heapq
import json
import math
from pathlib import Path
import subprocess
import sys

import numpy as np

from .output_selection import valid_output_run_name


SCHEMA = "net-distance-ph/geographic-audit/v49"


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _write_csv(path, rows, columns):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _inside(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _resolve_run(workspace, requested=None):
    base = workspace / "variants" / "population_weighted" / "output"
    if requested is None:
        latest = _read_json(base / "latest_run.json")
        chosen = Path(latest["directory"])
        if not chosen.is_absolute():
            chosen = base / chosen
        expected_hash = latest["routing_hash"]
    else:
        latest = None
        chosen = Path(requested).expanduser()
        if not chosen.is_absolute():
            chosen = (Path.cwd() / chosen)
        expected_hash = None
    chosen = chosen.resolve()
    if not _inside(chosen, base) or chosen.parent != base.resolve():
        raise ValueError(f"Routing run directory must be an immediate child of {base}: {chosen}")
    town = chosen / "town"
    if not (town / "manifest.json").is_file():
        raise FileNotFoundError(f"An existing town matrix is needed for this audit: {town}")
    tm = _read_json(town / "manifest.json")
    routing_hash = tm["routing_hash"]
    if not isinstance(routing_hash, str) or len(routing_hash) != 64:
        raise ValueError("Malformed routing fingerprint")
    if ((expected_hash is not None and expected_hash != routing_hash) or
            not valid_output_run_name(chosen.name, routing_hash) or tm.get("level") != "town"):
        raise ValueError("Latest-run pointer, town manifest, and routing directory do not agree")
    # Old runs contain a historical province-only export; new runs may be
    # town-only or also contain a nationally exhaustive province_equivalent.
    preferred = chosen / "province_equivalent"
    historical = chosen / "province"
    optional = preferred if (preferred / "manifest.json").is_file() else historical
    pm = _read_json(optional / "manifest.json") if (optional / "manifest.json").is_file() else None
    if pm is not None and (pm.get("routing_hash") != routing_hash or
            pm.get("core_cache_key") != tm.get("core_cache_key") or
            pm.get("level") not in {"province", "province_equivalent"}):
        raise ValueError("Optional upper-tier manifest describes a different routing run")
    return chosen, tm, pm, routing_hash


def _population_units(repo, town_manifest):
    dataset = repo / "reference" / "population" / "default"
    p = dataset / "units.csv"
    if not p.is_file():
        raise FileNotFoundError(f"Normalized units table missing: {p}. Do not regenerate it during an audit.")
    source = town_manifest.get("population_source") or {}
    expected = source.get("units_sha256")
    if not expected:
        raise ValueError("Source manifest lacks units_sha256; cannot verify administrative mapping")
    actual = _sha256(p)
    if actual != expected:
        raise ValueError("Administrative units table differs from the run's population-source hash")
    # The population input hash is independently recorded. Check it, too.
    pop = dataset / "barangay_population.csv"
    pop_hash = source.get("population_sha256")
    if not pop_hash or not pop.is_file() or _sha256(pop) != pop_hash:
        raise ValueError("Population table differs from the run's population-source hash")
    units = _read_csv(p)
    return {r["psgc_code"]: r for r in units}, {"units_sha256": actual, "population_sha256": pop_hash}


def _read_crosswalk(path, known_units, expected_sha):
    if expected_sha is None:
        if path is not None:
            raise ValueError("Crosswalk supplied but original run did not use one")
        return {}
    if path is None:
        raise ValueError("Run used a province crosswalk. Supply --province-crosswalk with matching SHA-256.")
    path = Path(path).expanduser().resolve()
    if _sha256(path) != expected_sha:
        raise ValueError("Province crosswalk SHA-256 differs from original run")
    result = {}
    for row in _read_csv(path):
        town, province = row["town_psgc"], row["province_psgc"]
        if town in result or town not in known_units or known_units[town]["level"] not in ("city", "municipality"):
            raise ValueError(f"Invalid or repeated crosswalk town {town}")
        if province not in known_units or known_units[province]["level"] != "province":
            raise ValueError(f"Invalid crosswalk province {province}")
        if known_units[town]["province_psgc"] and known_units[town]["province_psgc"] != province:
            raise ValueError("Crosswalk overrides an existing province parent")
        result[town] = province
    return result


def _load_town_tables(run):
    directory = run / "town"
    units = _read_csv(directory / "units.csv")
    codes = [row["psgc_code"] for row in units]
    if len(set(codes)) != len(codes):
        raise ValueError("Duplicate town codes")
    pop = np.load(directory / "population_per_unit.npy", mmap_mode="r")
    modeled = np.load(directory / "modeled_population_per_unit.npy", mmap_mode="r")
    probability = np.load(directory / "reachable_pair_probability.npy", mmap_mode="r")
    n = len(units)
    if (pop.shape != (n,) or modeled.shape != (n,) or probability.shape != (n, n)):
        raise ValueError("Population/reachability array shapes do not match town units")
    if (not np.isfinite(pop).all() or not np.isfinite(modeled).all() or
            (pop < 0).any() or (modeled < 0).any() or (modeled > pop + 1e-6).any() or
            not np.isfinite(probability).all() or (probability < -1e-8).any() or
            (probability > 1 + 1e-8).any()):
        raise ValueError("Invalid town population or reachability probability")
    if not np.allclose(probability, probability.T, atol=1e-8, rtol=0):
        raise ValueError("Asymmetric reachable-pair probability")
    return units, pop, modeled, probability


def province_exclusions(units, all_units, pop, modeled, crosswalk):
    """Identify modeled and unmodeled population excluded from province rollup."""
    province_rows = []
    for i, town in enumerate(units):
        code = town["psgc_code"]
        if code not in all_units:
            raise ValueError(f"Town {code} absent from hashed administrative units table")
        ref = all_units[code]
        if ref["level"] not in ("city", "municipality"):
            raise ValueError(f"Non-city/municipality in town matrix: {code}")
        province = crosswalk.get(code, ref["province_psgc"])
        if province:
            continue
        region = all_units.get(ref["region_psgc"], {})
        province_rows.append({
            "town_psgc": code, "town_name": town["name"], "classification": ref["level"],
            "region_psgc": ref["region_psgc"], "region_name": region.get("name", ""),
            "province_psgc": "", "reason": "no province_psgc in audited input mapping",
            "input_population": int(round(float(pop[i]))),
            "modeled_population": int(round(float(modeled[i]))),
            "unmodeled_population": int(round(float(pop[i] - modeled[i]))),
        })
    return province_rows


def _top_records(records, limit, key):
    return sorted(records, key=lambda r: (-r[key], r["town_a_psgc"], r["town_b_psgc"]))[:limit]


def pair_reachability(units, pop, modeled, coverage, *, top=100, all_pairs_path=None):
    """Summarize unordered inter-town pairs; split *modeled* unreachable from unmodeled.

    P(i,j) = reachable modeled population-pair mass / (input_pop_i * input_pop_j).
    Thus even a positive P does not establish that all modeled residents have a
    path. Population-pair masses below are floating-point model estimates.
    """
    n = len(units)
    pop = np.asarray(pop, dtype=np.float64)
    modeled = np.asarray(modeled, dtype=np.float64)
    coverage = np.asarray(coverage, dtype=np.float64)
    if coverage.shape != (n,n) or pop.shape != (n,) or modeled.shape != (n,):
        raise ValueError("Input dimensions disagree")
    total = n * (n-1) // 2
    stats = Counter()
    weights = defaultdict(float)
    unreachable = []  # fixed-size min-heap of (mass, tie breakers, record)
    partial = []

    def keep_largest(heap, record, metric, i, j):
        entry = (float(record[metric]), -i, -j, record)
        if len(heap) < top:
            heapq.heappush(heap, entry)
        elif entry[:3] > heap[0][:3]:
            heapq.heapreplace(heap, entry)
    csv_fh = None
    csv_writer = None
    tmp_csv = None
    csv_complete = False
    if all_pairs_path is not None:
        all_pairs_path = Path(all_pairs_path)
        all_pairs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_csv = all_pairs_path.with_name(all_pairs_path.name + ".tmp")
        csv_fh = tmp_csv.open("w", newline="", encoding="utf-8")
        csv_writer = csv.DictWriter(csv_fh, fieldnames=(
            "town_a_psgc", "town_a", "town_b_psgc", "town_b", "status",
            "coverage_probability", "modeled_reachability_fraction",
            "modeled_population_pair_mass", "reachable_modeled_population_pair_mass",
            "unreachable_modeled_population_pair_mass", "unmodeled_population_pair_mass"))
        csv_writer.writeheader()
    try:
        for i in range(n):
            for j in range(i+1, n):
                potential = float(pop[i] * pop[j])
                possible = float(modeled[i] * modeled[j])
                reachable = float(coverage[i,j] * potential)
                tol = max(1.0, possible) * 1e-7
                if reachable > possible + tol:
                    raise ValueError(f"Reachable mass exceeds modeled mass for town pair {i},{j}")
                reachable = min(max(reachable, 0.0), possible)
                lost = max(0.0, possible - reachable)
                unmodeled = max(0.0, potential - possible)
                weights["potential"] += potential
                weights["modeled"] += possible
                weights["reachable_modeled"] += reachable
                weights["unreachable_modeled"] += lost
                weights["unmodeled"] += unmodeled
                if possible == 0:
                    kind = "UNMODELED_PAIR"
                elif reachable <= tol:
                    kind = "ZERO_MODELED_REACHABILITY"
                elif lost <= tol:
                    kind = "COMPLETE_MODELED_REACHABILITY"
                else:
                    kind = "PARTIAL_MODELED_REACHABILITY"
                stats[kind] += 1
                record = {
                    "town_a_psgc": units[i]["psgc_code"], "town_a": units[i]["name"],
                    "town_b_psgc": units[j]["psgc_code"], "town_b": units[j]["name"],
                    "status": kind, "coverage_probability": round(float(coverage[i,j]), 9),
                    "modeled_reachability_fraction": round(reachable / possible, 9) if possible else None,
                    "modeled_population_pair_mass": round(possible, 2),
                    "reachable_modeled_population_pair_mass": round(reachable, 2),
                    "unreachable_modeled_population_pair_mass": round(lost, 2),
                    "unmodeled_population_pair_mass": round(unmodeled, 2),
                }
                if kind == "ZERO_MODELED_REACHABILITY":
                    keep_largest(unreachable, record, "modeled_population_pair_mass", i, j)
                elif kind == "PARTIAL_MODELED_REACHABILITY":
                    keep_largest(partial, record, "unreachable_modeled_population_pair_mass", i, j)
                if csv_writer is not None and kind != "COMPLETE_MODELED_REACHABILITY":
                    csv_writer.writerow(record)
        csv_complete = True
    finally:
        if csv_fh is not None:
            csv_fh.close()
            if csv_complete:
                tmp_csv.replace(all_pairs_path)
            else:
                tmp_csv.unlink(missing_ok=True)
    if sum(stats.values()) != total:
        raise AssertionError("Pair category counts do not sum to all unordered town pairs")
    if abs(weights["potential"] - weights["modeled"] - weights["unmodeled"]) > 1e-5 * max(1.,weights["potential"]):
        raise AssertionError("Population-pair mass decomposition is inconsistent")
    if abs(weights["modeled"] - weights["reachable_modeled"] - weights["unreachable_modeled"]) > 1e-5 * max(1.,weights["modeled"]):
        raise AssertionError("Reachability mass decomposition is inconsistent")
    summary = {
        "unordered_intertown_pairs": total,
        "pair_classification": dict(stats),
        "population_pair_mass": {k: round(v, 2) for k,v in weights.items()},
        "interpretation": "Two independent residents from two different towns; model pair masses are numerical estimates, not observed trips",
        "zero_reachability_means": "No demonstrated modeled route between population representatives; it is not proof of physically impossible travel",
        "partial_reachability_means": "Some but not all modeled origin-destination population pairs are mutually reachable",
        "unmodeled_means": "At least one side has population excluded from model snapping, separate from disconnected modeled people",
    }
    return summary, _top_records([x[3] for x in unreachable], top, "modeled_population_pair_mass"), _top_records([x[3] for x in partial], top, "unreachable_modeled_population_pair_mass")


def site_component_audit(sites, town_units, town_modeled):
    """One town may belong to many components; do not collapse these to town CCs."""
    sites_by_component = defaultdict(list)
    per_town = defaultdict(lambda: defaultdict(int))
    town_lookup = {r["psgc_code"]:r["name"] for r in town_units}
    for row in sites:
        town, comp = row["town_psgc"], int(row["component"])
        if town not in town_lookup:
            raise ValueError(f"Representative site references unknown town {town}")
        population = int(row["population"])
        if population <= 0:
            raise ValueError("Representative site with non-positive mass")
        sites_by_component[comp].append((town, population))
        per_town[town][comp] += population
    expected = {r["psgc_code"]:int(round(float(town_modeled[i]))) for i,r in enumerate(town_units)}
    for town, pop in expected.items():
        if sum(per_town[town].values()) != pop:
            raise ValueError(f"Site component population mismatch in town {town}: {sum(per_town[town].values())} vs {pop}")
    totals = sorted(sites_by_component, key=lambda c: (-sum(v for _,v in sites_by_component[c]),c))
    comp_rows = []
    for rank, comp in enumerate(totals, 1):
        records = sites_by_component[comp]
        towns = {t for t,_ in records}
        comp_rows.append({"component_rank": rank, "component_label": comp,
                          "population": sum(p for _,p in records), "town_count":len(towns),
                          "site_count":len(records), "sample_towns": "; ".join(f"{town_lookup[t]} ({t})" for t in sorted(towns)[:6])})
    memberships = []
    for code, name in sorted(town_lookup.items()):
        for comp, mass in sorted(per_town[code].items()):
            memberships.append({"town_psgc":code,"town_name":name,"component_label":comp,"modeled_population":mass})
    multi_towns = [t for t,parts in per_town.items() if len(parts)>1]
    return {"populated_components":len(comp_rows), "towns_in_multiple_components":len(multi_towns),
            "towns_without_modeled_sites":sum(not per_town[t] for t in town_lookup),
            "note":"Core-component IDs refer to the barangay-preserving graph, not stage-1 original graph component IDs. Multi-component towns cannot be assigned a unique component."}, comp_rows, memberships


def missing_ferry_gangways(flags, u, v, edge_codes, node_lonlat, official_towns):
    """Identify actual offshore terminal vertices lacking a direct road gangway.

    This reproduces the graph builder's terminal selection by flags and
    GANGWAY edge endpoints. It does not assume an absent gangway implies that
    the vertex is in a separate graph component or that a ferry is operating.
    """
    flags = np.asarray(flags)
    if flags.ndim != 2 or flags.shape[1] < 3:
        raise ValueError("Original graph is missing ferry-terminal flags")
    u, v, edge_codes = np.asarray(u), np.asarray(v), np.asarray(edge_codes)
    if u.shape != v.shape or u.shape != edge_codes.shape:
        raise ValueError("Graph edge arrays disagree")
    if np.asarray(node_lonlat).shape != (len(flags), 2):
        raise ValueError("Graph coordinate array shape disagrees")
    terminals = np.flatnonzero(flags[:,1] & flags[:,2] & ~flags[:,0])
    gangway = edge_codes == 2
    attached_nodes = np.unique(np.concatenate([u[gangway],v[gangway]]))
    attached = np.isin(terminals, attached_nodes)
    missing = terminals[~attached]
    road_nodes = flags[:,0]
    # The nearest official locality is geographic context, *not* the nearest road.
    valid = [(str(row["psgc_code"]),str(row.get("name","")),float(row["lon"]),float(row["lat"]))
             for row in official_towns if row.get("lon") not in (None, "") and row.get("lat") not in (None, "")]
    if valid:
        codes, names, xs, ys = zip(*valid)
        town_lon, town_lat = np.radians(np.asarray(xs)), np.radians(np.asarray(ys))
    else:
        codes, names = (), ()
    records = []
    for node in missing:
        lon, lat = map(float, node_lonlat[int(node)])
        closest, distance = "", None
        near_name = ""
        if valid:
            lon0, lat0 = math.radians(lon), math.radians(lat)
            dlat = town_lat - lat0
            dlon = town_lon - lon0
            hav = np.sin(dlat/2)**2 + np.cos(lat0)*np.cos(town_lat)*np.sin(dlon/2)**2
            dist = 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(hav, 0., 1.)))
            j = int(np.argmin(dist))
            closest,near_name,distance=codes[j],names[j],round(float(dist[j]), 3)
        records.append({"original_graph_node":int(node), "lon":lon, "lat":lat,
                        "nearest_official_town_psgc":closest,"nearest_official_town":near_name,
                        "straight_line_to_nearest_town_km":distance,
                        "interpretation":"No direct road overlap/gangway at this ferry terminal; may remain connected via the ferry graph"})
    return {"offshore_ferry_terminal_nodes":int(len(terminals)),
            "directly_gangway_attached_nodes":int(attached.sum()),
            "without_direct_gangway_nodes":len(records),
            "gangway_edges":int(np.count_nonzero(gangway)),
            "road_vertices":int(np.count_nonzero(road_nodes)),
            "note":"No direct gangway does not imply an isolated graph component. Nearest official town is geographic only, not a road distance."},records


def _audit_stage1(workspace, out):
    """Reuse the official auditor, preserving its strict source-provenance gate.

    A disconnected network correctly returns 1 after writing reports; this is
    a scientific finding, not an exception. Return code 2 means not ready.
    """
    from . import connectivity
    repo = Path(__file__).resolve().parents[1]
    args = argparse.Namespace(repo=repo, workspace=workspace, skip_provenance=False)
    rc = connectivity.execute(args, output_override=out)
    if rc not in (0,1):
        raise RuntimeError(f"Stage-1 connectivity auditor could not validate the current graph: status {rc}")
    report = _read_json(out / "connectivity_report.json")
    towns = _read_csv(out / "town_connectivity.csv")
    return report, towns


def execute(args):
    workspace = Path(args.workspace or (Path.home()/"Documents"/"net-distance-ph")).expanduser().resolve()
    run, town_manifest, province_manifest, routing_hash = _resolve_run(workspace, args.run_dir)
    repo = Path(__file__).resolve().parents[1]
    units, input_hashes = _population_units(repo, town_manifest)
    towns, input_pop, modeled_pop, probability = _load_town_tables(run)
    if int(town_manifest["n_units"]) != len(towns):
        raise ValueError("Town count differs from manifest")
    crosswalk = _read_crosswalk(args.province_crosswalk, units,
                               province_manifest.get("province_crosswalk_sha256") if province_manifest else None)
    exclusions = province_exclusions(towns, units, input_pop, modeled_pop, crosswalk)
    # Raw PSGC province-parent gaps are not actual exclusions from an
    # exhaustive province-equivalent export. Historical province-only runs
    # retain their original excluded-population consistency check.
    excluded_input = sum(r["input_population"] for r in exclusions)
    upper_tier = province_manifest.get("level") if province_manifest else None
    if upper_tier == "province":
        if not math.isclose(excluded_input, float(province_manifest["excluded_population_by_level"]), rel_tol=0, abs_tol=1):
            raise ValueError("Province-exclusion list disagrees with the saved province manifest")
    elif upper_tier == "province_equivalent":
        if not math.isclose(float(province_manifest["excluded_population_by_level"]),0,rel_tol=0,abs_tol=1):
            raise ValueError("Province-equivalent export cannot have excluded town population")
    out = workspace / "output" / "geographic_audit" / routing_hash[:16]
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "province_excluded_towns.csv", exclusions,
               ("town_psgc","town_name","classification","region_psgc","region_name","province_psgc",
                "reason","input_population","modeled_population","unmodeled_population"))
    if upper_tier=="province":
        print(f"LEGACY PROVINCE: {len(exclusions)} towns excluded; {sum(r['modeled_population'] for r in exclusions):,} modeled residents", flush=True)
    else:
        print(f"RAW PROVINCE HIERARCHY: {len(exclusions)} towns without provincial parent; output tier={upper_tier or 'town_only'}; these are NOT excluded from town output", flush=True)
    all_pairs = out / "all_noncomplete_town_pairs.csv" if args.all_pairs else None
    pair_summary, zeros, partial = pair_reachability(towns, input_pop, modeled_pop, probability,
                                                     top=args.top, all_pairs_path=all_pairs)
    pair_fields = ("town_a_psgc","town_a","town_b_psgc","town_b","status","coverage_probability",
                   "modeled_reachability_fraction","modeled_population_pair_mass",
                   "reachable_modeled_population_pair_mass","unreachable_modeled_population_pair_mass","unmodeled_population_pair_mass")
    _write_csv(out / "top_zero_reachability_town_pairs.csv", zeros, pair_fields)
    _write_csv(out / "top_partial_reachability_town_pairs.csv", partial, pair_fields)
    print(f"PAIRS: {pair_summary['pair_classification']}", flush=True)
    site_path = run / "representative_sites.csv"
    sites = _read_csv(site_path)
    if len(sites) != int(town_manifest["n_sites"]):
        raise ValueError("Representative site count differs from manifest")
    comp_summary, components, memberships = site_component_audit(sites,towns,modeled_pop)
    _write_csv(out / "population_components.csv", components,
               ("component_rank","component_label","population","town_count","site_count","sample_towns"))
    _write_csv(out / "town_component_memberships.csv", memberships,
               ("town_psgc","town_name","component_label","modeled_population"))
    print(f"POPULATION COMPONENTS: {comp_summary['populated_components']} components, {comp_summary['towns_in_multiple_components']} multi-component towns",flush=True)
    original_conn = None
    terminals = None
    if not args.skip_graph:
        conn_path = out / "stage1_connectivity"
        original_conn, town_conn = _audit_stage1(workspace,conn_path)
        graph = workspace / "work" / "graph_full.npz"
        with np.load(graph, allow_pickle=False) as g:
            # Copy only needed arrays from compressed archive; not the weights.
            flags = g["flags"]
            u, v, edge_codes = g["u"],g["v"],g["code"]
            coords = g["node_lonlat"]
        terminals, orphan_rows = missing_ferry_gangways(flags,u,v,edge_codes,coords,town_conn)
        _write_csv(out / "unattached_ferry_terminals.csv", orphan_rows,
                   ("original_graph_node","lon","lat","nearest_official_town_psgc","nearest_official_town",
                    "straight_line_to_nearest_town_km","interpretation"))
        print(f"FERRY: {terminals['without_direct_gangway_nodes']} offshore terminals without direct gangway",flush=True)
        print(f"ORIGINAL GRAPH: {original_conn['official_components_road_only']} road-only town components; {original_conn['official_components_road_ferry']} with ferries",flush=True)
    report={
        "schema":SCHEMA, "routing_hash":routing_hash, "run_directory":str(run),
        "source_input_sha256":input_hashes,
        "historical_run_code_commit": None,
        "province_accounting": {"output_level":upper_tier,
            "towns_without_raw_province_parent":len(exclusions),
            "population_without_raw_province_parent":excluded_input,
            "excluded_town_count":(len(exclusions) if upper_tier=="province" else 0 if upper_tier=="province_equivalent" else None),
            "excluded_input_population":(excluded_input if upper_tier=="province" else 0 if upper_tier=="province_equivalent" else None),
            "excluded_modeled_population":(sum(r["modeled_population"] for r in exclusions) if upper_tier=="province" else 0 if upper_tier=="province_equivalent" else None),
            "excluded_unmodeled_population":(sum(r["unmodeled_population"] for r in exclusions) if upper_tier=="province" else 0 if upper_tier=="province_equivalent" else None),
            "province_rollup_is_nationally_complete":(False if exclusions else True) if upper_tier=="province" else True if upper_tier=="province_equivalent" else None,
            "note":"CSV lists towns without a raw provincial parent, not necessarily towns excluded from the selected output. Province-equivalent exports include nonprovincial cities and municipalities as peers; None indicates not computed."},
        "population_pair_reachability":pair_summary,
        "population_site_components":comp_summary,
        "original_stage1_connectivity":original_conn,
        "ferry_terminal_connections":terminals,
        "files": {"province_exclusions":"province_excluded_towns.csv",
                  "population_component_summary":"population_components.csv",
                  "town_component_memberships":"town_component_memberships.csv",
                  "top_zero_reachability":"top_zero_reachability_town_pairs.csv",
                  "top_partial_reachability":"top_partial_reachability_town_pairs.csv",
                  "all_noncomplete_pairs":"all_noncomplete_town_pairs.csv" if args.all_pairs else None,
                  "original_town_connectivity":"stage1_connectivity/town_connectivity.csv" if original_conn else None,
                  "original_component_summary":"stage1_connectivity/component_summary.csv" if original_conn else None,
                  "original_barrier_examples":"stage1_connectivity/barrier_examples.csv" if original_conn else None,
                  "unattached_ferry_terminals":"unattached_ferry_terminals.csv" if terminals else None},
        "scientific_scope": {
            "status":"MODELED_TOPOLOGY_AND_POPULATION_ACCOUNTING_ONLY",
            "road_ferry_paths":"Undirected model paths, not an operating ferry timetable or proof of a usable journey",
            "legal_province_status":"Not assessed from provincial_parent = empty",
            "stage1_vs_population":"Original-town centroid components and barangay-site components are distinct and not interchangeable",
            "numerical_distance_verification":"Not rerun; only existing reachability matrices and graph connectivity used",
            "core_connectivity":"Based on representative site component labels saved by the population routing run",
        },
    }
    report_path=out / "geographic_audit.json"
    _write_json(report_path,report)
    print(f"AUDIT REPORT: {report_path}",flush=True)
    return 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path, default=Path.home()/"Documents"/"net-distance-ph")
    p.add_argument("--run-dir", type=Path, help="explicit existing population-weighted run output directory (default: latest)")
    p.add_argument("--province-crosswalk", type=Path, help="original signed crosswalk if run used one")
    p.add_argument("--top", type=int, default=100, help="top unreachable and partial pairs exported to CSV (default 100)")
    p.add_argument("--all-pairs", action="store_true", help="also export all zero, partial, and unmodeled town pairs (can be large)")
    p.add_argument("--skip-graph", action="store_true", help="skip the original 12m-node connectivity/ferry-terminal audit")
    args=p.parse_args(argv)
    if args.top < 1 or args.top > 100000:
        p.error("--top must be between 1 and 100000")
    try:
        return execute(args)
    except (ValueError, FileNotFoundError, KeyError, OSError, RuntimeError) as exc:
        print(f"GEOGRAPHIC AUDIT REFUSED: {exc}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
