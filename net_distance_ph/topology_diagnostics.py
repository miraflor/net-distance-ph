"""Read-only audit of why population-network components do not connect.

Compares the original undirected stage-1 graph against the barangay-preserving
contracted graph using *actual selected representative nodes*. Finds candidate
short geometric gaps in the saved original graph; NEVER inserts edges, assigns
travel feasibility, or performs shortest path routing.

The geographic search reports a closest candidate *inside the specified search
window*, not a globally nearest connection when no candidate is found.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import math
from pathlib import Path

import numpy as np
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from . import geographic_audit as ga
from . import matrix as mx
from .connectivity import labels_for_graph
from .geo import geodesic_km, lonlat_to_ecef

SCHEMA = "net-distance-ph/topology-diagnostics/v50"


def _site_rows(path):
    rows = ga._read_csv(path)
    required = {"town_psgc", "core_node", "original_node", "population", "component", "lon", "lat"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("Representative sites CSV is missing required fields")
    result = []
    for r in rows:
        result.append({**r, "core_node": int(r["core_node"]),
                       "original_node": int(r["original_node"]),
                       "population": int(r["population"]),
                       "component": int(r["component"]),
                       "lon": float(r["lon"]), "lat": float(r["lat"])})
    if any(r["population"] <= 0 for r in result):
        raise ValueError("Representative site has nonpositive population")
    return result


def compare_component_lineage(sites, core_labels, original_labels, original_coords=None):
    """Test connectivity preservation for all selected population representatives.

    Bijection holds between original and reduced component *labels that have
    protected representatives*. Other graph-only components may be dropped.
    """
    core_labels = np.asarray(core_labels)
    original_labels = np.asarray(original_labels)
    core_to_original, original_to_core = defaultdict(set), defaultdict(set)
    core_mass, site_counts, original_mass = Counter(), Counter(), Counter()
    for site in sites:
        a, b = site["core_node"], site["original_node"]
        if not (0 <= a < len(core_labels) and 0 <= b < len(original_labels)):
            raise ValueError("Representative site references an out-of-range graph node")
        core = int(core_labels[a])
        original = int(original_labels[b])
        if core != site["component"]:
            raise ValueError(f"Stored component label differs from recomputed core: {site['town_psgc']}, original {b}")
        if original_coords is not None:
            x, y = original_coords[b]
            if not np.allclose((x, y), (site["lon"], site["lat"]), rtol=0, atol=0.000002):
                raise ValueError(f"Representative site coordinates differ from original snapped node: {b}")
        core_to_original[core].add(original)
        original_to_core[original].add(core)
        core_mass[core] += site["population"]
        original_mass[original] += site["population"]
        site_counts[core] += 1
    split_original = {c: sorted(k) for c, k in original_to_core.items() if len(k) > 1}
    merged_original = {c: sorted(k) for c, k in core_to_original.items() if len(k) > 1}
    equivalence = not split_original and not merged_original
    mapping = {c: next(iter(v)) for c, v in core_to_original.items() if len(v) == 1}
    return {
        "preserved_for_selected_representatives": equivalence,
        "selected_core_components": len(core_to_original),
        "selected_original_components": len(original_to_core),
        "original_components_split_by_reduction": split_original,
        "original_components_merged_by_reduction": merged_original,
        "representative_sites_tested": len(sites),
        "population_tested": sum(core_mass.values()),
        "scope": "Equivalence of connectivity labels for selected sites; this is not full shortest-path equivalence or real-world validation",
    }, mapping, core_mass, site_counts


def town_component_rows(sites, towns, original_for_core, centroid_labels=None, dominant_core=None):
    """Expose cross-town access on the same component, not merely CC counts."""
    by_town_comp = defaultdict(list)
    component_towns = defaultdict(set)
    for s in sites:
        key = (s["town_psgc"], s["component"])
        by_town_comp[key].append(s)
        component_towns[s["component"]].add(s["town_psgc"])
    names = {t["psgc_code"]: t["name"] for t in towns}
    per_town = defaultdict(int)
    for (town, _), records in by_town_comp.items():
        per_town[town] += sum(x["population"] for x in records)
    rows = []
    for (town, comp), records in sorted(by_town_comp.items()):
        original = original_for_core.get(comp)
        centroid = centroid_labels.get(town) if centroid_labels else None
        others = sorted(component_towns[comp] - {town})
        population = sum(s["population"] for s in records)
        rows.append({
            "town_psgc": town, "town_name": names[town],
            "core_component": comp, "original_component": original,
            "component_population_in_town": population,
            "town_modeled_population": per_town[town],
            "town_population_fraction": round(population / per_town[town], 9),
            "representative_sites": len(records),
            "component_town_count": len(component_towns[comp]),
            "other_towns_in_component": "; ".join(names[t] + " (" + t + ")" for t in others[:12]),
            "other_towns_omitted": max(0, len(others) - 12),
            "centroid_original_component": centroid,
            "same_component_as_town_centroid": (centroid == original if centroid is not None and original is not None else None),
            "same_component_as_national_population_majority": comp == dominant_core,
        })
    return rows


def _node_kind(flag):
    # flags columns: road, ferry, ferry-part terminal
    s = []
    if flag[0]: s.append("road")
    if flag[1]: s.append("ferry")
    if len(flag) > 2 and flag[2]: s.append("ferry_endpoint")
    return "+".join(s) or "unclassified"


def nearest_other_component_gap(lonlat, flags, labels, component, *, search_km=10.0,
                                target_component=None, max_source_nodes=300000,
                                max_nearby_nodes=2000000, degrees=None):
    """Locate the shortest geometric cross-component node pair within window.

    Expand the component bounding box by search_km, take eligible targets
    outside this graph component, and compare ALL source nodes to all eligible
    nodes using nearest-neighbour ECEF/chord distances. The bounding pad is
    conservative in Philippines-like latitude ranges. Does not create edges.
    """
    xy = np.asarray(lonlat)
    flags = np.asarray(flags)
    labels = np.asarray(labels)
    if search_km <= 0 or not math.isfinite(search_km):
        raise ValueError("search_km must be finite and positive")
    if len(xy) != len(labels) or flags.shape[0] != len(xy):
        raise ValueError("Graph topology and coordinates are inconsistent")
    source = np.flatnonzero(labels == component)
    if source.size == 0:
        return {"status": "NO_SOURCE_NODES", "source_nodes": 0}
    if source.size > max_source_nodes:
        return {"status": "TOO_MANY_SOURCE_NODES", "source_nodes": int(source.size)}
    sxy = xy[source]
    minlat, maxlat = float(sxy[:, 1].min()), float(sxy[:, 1].max())
    lat_pad = search_km / 110.5  # conservatively large for WGS84 meridian radius
    cos_min = min(math.cos(math.radians(max(-89.99, minlat - lat_pad))),
                  math.cos(math.radians(min(89.99, maxlat + lat_pad))))
    lon_pad = search_km / (110.5 * max(0.05, cos_min))
    if lon_pad > 180: lon_pad = 180
    good = ((xy[:, 0] >= float(sxy[:, 0].min()) - lon_pad) &
            (xy[:, 0] <= float(sxy[:, 0].max()) + lon_pad) &
            (xy[:, 1] >= minlat - lat_pad) &
            (xy[:, 1] <= maxlat + lat_pad) & (labels != component))
    if target_component is not None:
        good &= labels == target_component
    candidates = np.flatnonzero(good)
    if candidates.size == 0:
        return {"status": "NO_OTHER_COMPONENT_WITHIN_SEARCH_WINDOW", "source_nodes": int(source.size),
                "candidate_nodes": 0}
    if candidates.size > max_nearby_nodes:
        return {"status": "TOO_MANY_CANDIDATES", "source_nodes": int(source.size),
                "candidate_nodes": int(candidates.size)}
    tree = cKDTree(lonlat_to_ecef(xy[candidates, 0], xy[candidates, 1]))
    chord, near = tree.query(lonlat_to_ecef(sxy[:, 0], sxy[:, 1]), k=1)
    i = int(np.argmin(chord))
    j = int(candidates[int(near[i])])
    src = int(source[i])
    gap_km = float(geodesic_km(xy[src, 0], xy[src, 1], xy[j, 0], xy[j, 1]))
    # The rectangle is conservative; a picked candidate may still lie farther
    # than the stated radius from every source node.
    status = "CANDIDATE_WITHIN_RADIUS" if gap_km <= search_km + 1e-8 else "NO_NODE_WITHIN_RADIUS"
    return {
        "status": status, "source_nodes": int(source.size),
        "candidate_nodes": int(candidates.size),
        "geometric_gap_km": round(gap_km, 6) if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "source_node": src if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "source_lon": float(xy[src, 0]) if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "source_lat": float(xy[src, 1]) if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "source_node_kind": _node_kind(flags[src]) if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "source_degree": int(degrees[src]) if degrees is not None and status == "CANDIDATE_WITHIN_RADIUS" else None,
        "candidate_node": j if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "candidate_lon": float(xy[j, 0]) if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "candidate_lat": float(xy[j, 1]) if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "candidate_original_component": int(labels[j]) if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "candidate_node_kind": _node_kind(flags[j]) if status == "CANDIDATE_WITHIN_RADIUS" else None,
        "candidate_degree": int(degrees[j]) if degrees is not None and status == "CANDIDATE_WITHIN_RADIUS" else None,
        "interpretation": "Proximity only. Check roads/bridges/water, OSM identities, and actual ferry terminals before proposing an edge",
    }


def execute(args):
    """Read existing source, graph and core, write reports only."""
    import time
    started = time.monotonic()
    workspace = Path(args.workspace or (Path.home() / "Documents" / "net-distance-ph")).expanduser().resolve()
    run, manifest, _, routing_hash = ga._resolve_run(workspace, args.run_dir)
    repo = Path(__file__).resolve().parents[1]
    all_units, hashes = ga._population_units(repo, manifest)
    towns, _, modeled, _ = ga._load_town_tables(run)
    sites = _site_rows(run / "representative_sites.csv")
    core_key = manifest["core_cache_key"]
    core = workspace / "variants" / "population_weighted" / "cores" / core_key[:16]
    metadata = ga._read_json(core / "metadata.json")
    if metadata.get("cache_key") != core_key or metadata.get("population_sha256") != hashes["population_sha256"] or metadata.get("units_sha256") != hashes["units_sha256"]:
        raise ValueError("Population core metadata does not match original route manifests and hashed population tables")
    graph = workspace / "work" / "graph_full.npz"
    actual_graph_hash = ga._sha256(graph)
    if metadata.get("national_graph_sha256") != actual_graph_hash:
        raise ValueError("Original graph file has changed since population core construction; refuse cross-run topology conclusions")
    graph_fingerprint = None
    with np.load(workspace / "work" / "sources.npz", allow_pickle=False) as src:
        import json
        source_manifest = json.loads(str(src["input_manifest"]))
        graph_fingerprint = source_manifest.get("fingerprint")
        source_codes = [str(v) for v in src["ids"]]
        source_node_index = np.asarray(src["node_idx"], dtype=np.int64)
        source_tier = np.asarray(src["tier"])
    if graph_fingerprint != metadata.get("graph_source_fingerprint"):
        raise ValueError("Original graph-source fingerprints differ from the population core")
    print("PROVENANCE: original graph bytes, core lineage, and population input hashes agree", flush=True)
    from .workspace import timed_step
    with timed_step("check original and reduced graph connectivity"):
        core_graph = mx.load_core(core, mmap=True)
        _, core_labels = connected_components(core_graph, directed=False)
        del core_graph
        with np.load(graph, allow_pickle=False) as orig:
            u = orig["u"]
            v = orig["v"]
            flags = orig["flags"]
            lonlat = orig["node_lonlat"]
        count_full, original_labels = labels_for_graph(len(flags), u, v)
        lineage, mapping, core_mass, core_site_count = compare_component_lineage(sites, core_labels, original_labels, lonlat)
        del core_labels
    by_code = {v: i for i, v in enumerate(source_codes)}
    centroids = {}
    for t in towns:
        pos = by_code.get(t["psgc_code"])
        if pos is not None and source_tier[pos] >= 0:
            centroids[t["psgc_code"]] = int(original_labels[int(source_node_index[pos])])
    dominant = max(core_mass, key=lambda k: (core_mass[k], -k))
    rows = town_component_rows(sites, towns, mapping, centroid_labels=centroids, dominant_core=dominant)
    totals_by_town = Counter()
    for row in rows:
        totals_by_town[row["town_psgc"]] += row["component_population_in_town"]
    for town, mass in zip(towns, modeled):
        computed = totals_by_town[town["psgc_code"]]
        if not np.isclose(computed, float(mass), rtol=0, atol=0.5):
            raise ValueError("Saved population mass differs from representative-site membership")
    # Component summary by descending modeled population, including geographic
    # cases with multiple towns on the SAME isolated component.
    comp_towns = defaultdict(set)
    for r in rows: comp_towns[r["core_component"]].add(r["town_psgc"])
    chosen = sorted((c for c in core_mass if c != dominant), key=lambda c: (-core_mass[c], c))[:args.top_components]
    pop_rows = []
    for rank, c in enumerate(chosen, 1):
        town_set = sorted(comp_towns[c])
        pop_rows.append({"rank": rank, "core_component": c,
            "original_component": mapping.get(c), "modeled_population": core_mass[c],
            "sites": core_site_count[c], "town_count": len(town_set),
            "towns": "; ".join(f"{all_units[t]['name']} ({t})" for t in town_set[:20]),
            "additional_towns_not_shown": max(0, len(town_set) - 20)})
    gap_rows = []
    if not lineage["preserved_for_selected_representatives"]:
        print("REDUCTION INVARIANT FAILURE: stopped before interpreting spatial gaps", flush=True)
    elif args.lineage_only:
        print("LINEAGE ONLY: skipped candidate-gap scans", flush=True)
    elif chosen:
        degrees = (np.bincount(u, minlength=len(flags)) + np.bincount(v, minlength=len(flags)))
        del u, v
        main_original = mapping[dominant]
        for rank, c in enumerate(chosen, 1):
            orig_c = mapping[c]
            town_set = sorted(comp_towns[c])
            print(f"GAP {rank}/{len(chosen)}: {core_mass[c]:,} residents, {len(town_set)} towns; checking nearby graph vertices", flush=True)
            for relation, target in (("nearest_any_other_component", None), ("nearest_national_main_component", main_original)):
                result = nearest_other_component_gap(lonlat, flags, original_labels, orig_c,
                    search_km=args.search_km, target_component=target, degrees=degrees)
                gap_rows.append({"rank": rank, "core_component": c, "original_component": orig_c,
                                "modeled_population": core_mass[c], "relation": relation,
                                "search_radius_km": args.search_km, **result})
    elif not chosen:
        print("No nonmain populated components to diagnose", flush=True)
    root = workspace / "output" / "topology_diagnostics" / routing_hash[:16]
    root.mkdir(parents=True, exist_ok=True)
    cols = ["town_psgc", "town_name", "core_component", "original_component",
            "component_population_in_town", "town_modeled_population", "town_population_fraction",
            "representative_sites", "component_town_count", "other_towns_in_component", "other_towns_omitted",
            "centroid_original_component", "same_component_as_town_centroid",
            "same_component_as_national_population_majority"]
    ga._write_csv(root / "town_component_access.csv", rows, cols)
    site_export = [{"town_psgc": s["town_psgc"], "town_name": all_units[s["town_psgc"]]["name"],
         "population": s["population"], "lon": s["lon"], "lat": s["lat"],
         "core_node": s["core_node"], "original_node": s["original_node"],
         "core_component": s["component"],
         "original_component": int(original_labels[s["original_node"]]),
         "centroid_original_component": centroids.get(s["town_psgc"]),
         "same_component_as_centroid": int(original_labels[s["original_node"]]) == centroids.get(s["town_psgc"]),
         "in_main_population_component": s["component"] == dominant}
         for s in sites]
    ga._write_csv(root / "selected_site_components.csv", site_export,
       ["town_psgc", "town_name", "population", "lon", "lat", "core_node", "original_node",
        "core_component", "original_component", "centroid_original_component",
        "same_component_as_centroid", "in_main_population_component"])
    ga._write_csv(root / "priority_components.csv", pop_rows,
                  ["rank", "core_component", "original_component", "modeled_population", "sites", "town_count", "towns", "additional_towns_not_shown"])
    gap_cols = ["rank", "core_component", "original_component", "modeled_population", "relation", "search_radius_km",
       "status", "source_nodes", "candidate_nodes", "geometric_gap_km", "source_node", "source_lon",
       "source_lat", "source_node_kind", "source_degree", "candidate_node", "candidate_lon", "candidate_lat",
       "candidate_original_component", "candidate_node_kind", "candidate_degree", "interpretation"]
    ga._write_csv(root / "candidate_geometric_gaps.csv", gap_rows, gap_cols)
    report = {"schema": SCHEMA, "routing_hash": routing_hash, "stage1_graph_sha256": actual_graph_hash,
              "stage1_source_fingerprint": graph_fingerprint, "population_input_hashes": hashes,
              "lineage": lineage, "main_population_core_component": dominant,
              "main_original_graph_component": mapping.get(dominant),
              "all_original_graph_components": count_full,
              "towns_with_multiple_components": sum(n > 1 for n in Counter(r["town_psgc"] for r in rows).values()),
              "centroid_disagrees_with_site_component_rows": sum(r["same_component_as_town_centroid"] is False for r in rows),
              "centroid_disagrees_but_other_town_shares_component": sum(
                  r["same_component_as_town_centroid"] is False and r["component_town_count"] > 1 for r in rows),
              "priority_components_requested": args.top_components,
              "geometric_search_km": args.search_km,
              "geometric_gap_status": ("NOT_RUN_REDUCTION_MISMATCH" if not lineage["preserved_for_selected_representatives"]
                     else "NOT_REQUESTED" if args.lineage_only else "COMPLETED"),
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "files": {"town_component_access": "town_component_access.csv",
                        "selected_site_components": "selected_site_components.csv",
                        "priority_components": "priority_components.csv",
                        "candidate_geometric_gaps": "candidate_geometric_gaps.csv"},
              "limitations": ["Graph connectivity equality at selected sites does not establish shortest-path-distance equality",
                   "A short geometric gap is not permission to connect road or ferry edges",
                   "Candidate gaps are searched only within the stated radius; no candidate does not prove geographic isolation",
                   "Existing undirected graph models do not establish legal ferry services, usable bridges, or turn restrictions"]}
    ga._write_json(root / "topology_diagnostics.json", report)
    print(f"CROSS-TOWN: {report['centroid_disagrees_but_other_town_shares_component']} town-component memberships "
          f"share other towns while differing from own centroid", flush=True)
    print(f"REDUCTION: {'PASS' if lineage['preserved_for_selected_representatives'] else 'FAIL'}; "
          f"{lineage['selected_core_components']} selected core components; "
          f"{lineage['selected_original_components']} selected original components", flush=True)
    print(f"TOPOLOGY REPORT: {root / 'topology_diagnostics.json'}", flush=True)
    return 0 if lineage["preserved_for_selected_representatives"] else 1
