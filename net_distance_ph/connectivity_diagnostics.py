"""Read-only geometric diagnostics of disconnected national road/ferry components.

Identifies *candidate* short gaps between graph vertices. A short geodesic gap
never proves a traversable connection: OSM road classes, ways, grade separation,
intersections, bridges, coastline, and ferry boarding all need source review.
No edges or source/graph checkpoints are changed.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import numpy as np
from scipy.spatial import cKDTree

from .connectivity import labels_for_graph
from .geo import geodesic_km, lonlat_to_ecef


def _geodesic_pair(a, b):
    return float(geodesic_km(float(a[0]), float(a[1]), float(b[0]), float(b[1])))


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, columns)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def analyze_graph(graph: dict, sources: dict):
    """Compute locality-component summaries using only existing stage-1 arrays.

    Every intercomponent distance is between existing graph *vertices*, not
    road-segment interiors, and does not imply a viable transport connection.
    """
    coords = np.asarray(graph["node_lonlat"], dtype=np.float64)
    flags = np.asarray(graph["flags"], dtype=bool)
    u = np.asarray(graph["u"], dtype=np.int64)
    v = np.asarray(graph["v"], dtype=np.int64)
    code = np.asarray(graph["code"], dtype=np.int8)
    idx = np.asarray(sources["node_idx"], dtype=np.int64)
    tiers = np.asarray(sources["tier"], dtype=np.int8)
    offsets = np.asarray(sources["offset"], dtype=float)
    ids = [str(x) for x in sources["ids"]]
    names = [str(x) for x in sources["names"]]
    origin_xy = np.asarray(sources["lonlat"], dtype=np.float64)
    n = len(coords)
    if n == 0 or coords.shape != (n, 2):
        raise ValueError("Graph has an invalid node_lonlat array")
    if flags.shape[0] != n or flags.ndim != 2 or flags.shape[1] < 2:
        raise ValueError("Graph has invalid node flags")
    if not (len(idx) == len(tiers) == len(offsets) == len(ids) == len(names) == len(origin_xy)):
        raise ValueError("Source arrays have inconsistent lengths")
    if origin_xy.shape != (len(idx), 2):
        raise ValueError("Source longitude/latitude has unexpected dimensions")
    if u.shape != v.shape or u.shape != code.shape:
        raise ValueError("Graph edge arrays are inconsistent")
    snapped = tiers >= 0
    if np.any(idx[snapped] < 0) or np.any(idx[snapped] >= n):
        raise ValueError("Invalid snapped node index")
    if not np.isin(code, [0, 1, 2]).all():
        raise ValueError("Unknown graph edge type")
    if not np.isfinite(coords).all():
        raise ValueError("Graph coordinates contain non-finite values")
    if not snapped.any():
        raise ValueError("No snapped localities")

    n_graph_components, labels = labels_for_graph(n, u, v)
    members: dict[int, list[int]] = defaultdict(list)
    for j in np.flatnonzero(snapped):
        members[int(labels[idx[j]])].append(int(j))
    sorted_comps = sorted(members, key=lambda c: (-len(members[c]), min(ids[j] for j in members[c])))
    display = {c: f"C{k:04d}" for k, c in enumerate(sorted_comps, 1)}
    main = sorted_comps[0]

    road = flags[:, 0]
    main_road_nodes = np.flatnonzero(road & (labels == main))
    if not main_road_nodes.size:
        raise ValueError("Main component contains no road nodes")
    # Three-dimensional ECEF avoids distortion at the edges of the Philippines.
    # One shared KD tree avoids doing a separate multi-million-node search
    # for every smaller component. Query only small-component road vertices.
    print(f"BUILDING DIAGNOSTIC INDEX: {main_road_nodes.size:,} main-component road vertices", flush=True)
    tree = cKDTree(lonlat_to_ecef(coords[main_road_nodes, 0], coords[main_road_nodes, 1]))
    per_comp_nodes = np.bincount(labels, minlength=n_graph_components)
    per_comp_edges = [np.bincount(labels[u[code == k]], minlength=n_graph_components) for k in (0, 1, 2)]
    rows = []
    geojson_features = []
    nearest_by_comp = {}

    for comp in sorted_comps[1:]:
        candidates = np.flatnonzero(road & (labels == comp))
        sample_towns = sorted(members[comp], key=lambda j: ids[j])
        base = {
            "component": display[comp],
            "town_count": len(members[comp]),
            "town_names": "; ".join(f"{names[j]} ({ids[j]})" for j in sample_towns),
            "component_nodes": int(per_comp_nodes[comp]),
            "road_nodes": int(candidates.size),
            "road_edges": int(per_comp_edges[0][comp]),
            "ferry_edges": int(per_comp_edges[1][comp]),
            "gangway_edges": int(per_comp_edges[2][comp]),
        }
        if candidates.size:
            ecef = lonlat_to_ecef(coords[candidates, 0], coords[candidates, 1])
            chord, k = tree.query(ecef, k=1)
            pick = int(np.argmin(chord))
            from_id = int(candidates[pick])
            to_id = int(main_road_nodes[k[pick]])
            start, end = coords[from_id], coords[to_id]
            gap_km = _geodesic_pair(start, end)
            base.update({
                "nearest_main_road_vertex_km": round(gap_km, 6),
                "from_node": from_id, "to_main_node": to_id,
                "from_lon": float(start[0]), "from_lat": float(start[1]),
                "to_lon": float(end[0]), "to_lat": float(end[1]),
            })
            geojson_features.append({
                "type": "Feature",
                "properties": {"component": display[comp], "town_count": len(sample_towns),
                               "road_vertex_gap_km": round(gap_km, 6),
                               "interpretation": "Straight-line diagnostic only: not a proposed or proven road link"},
                "geometry": {"type": "LineString", "coordinates": [start.tolist(), end.tolist()]},
            })
            nearest_by_comp[comp] = gap_km
        else:
            base.update({"nearest_main_road_vertex_km": "", "from_node": "", "to_main_node": "",
                         "from_lon": "", "from_lat": "", "to_lon": "", "to_lat": ""})
        rows.append(base)

    # Compare the road node each town was *actually* attached to with the
    # distance to the closest main-component road vertex. This can expose
    # snap-choice issues without reattaching any locality.
    target = [j for j in range(len(idx)) if not snapped[j] or int(labels[idx[j]]) != main]
    town_rows = []
    if target:
        ecef = lonlat_to_ecef(origin_xy[target, 0], origin_xy[target, 1])
        _, main_match = tree.query(ecef, k=1)
        for t, j in enumerate(target):
            dest_id = int(main_road_nodes[main_match[t]])
            km = _geodesic_pair(origin_xy[j], coords[dest_id])
            comp_id = display[int(labels[idx[j]])] if snapped[j] else ""
            town_rows.append({
                "psgc_code": ids[j], "name": names[j],
                "state": "SNAPPED" if snapped[j] else "UNSNAPPABLE",
                "component": comp_id,
                "actual_snap_tier": int(tiers[j]),
                "actual_snap_offset_km": round(float(offsets[j]), 6) if snapped[j] else "",
                "origin_lon": float(origin_xy[j, 0]), "origin_lat": float(origin_xy[j, 1]),
                "nearest_main_road_vertex_km": round(km, 6),
                "nearest_main_road_node": dest_id,
                "nearest_main_road_lon": float(coords[dest_id, 0]),
                "nearest_main_road_lat": float(coords[dest_id, 1]),
            })

    rows.sort(key=lambda r: (float(r["nearest_main_road_vertex_km"]) if r["nearest_main_road_vertex_km"] != "" else float("inf"), r["component"]))
    town_rows.sort(key=lambda r: (r["state"] != "SNAPPED", r["nearest_main_road_vertex_km"], r["psgc_code"]))
    summary = {
        "schema": "net-distance-ph/connectivity-diagnostics/v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "DIAGNOSTIC_ONLY_NO_GRAPH_EDITS",
        "locality_count": len(ids), "snapped_count": int(snapped.sum()),
        "unsnappable_count": int((~snapped).sum()),
        "main_component": display[main],
        "main_component_towns": len(members[main]),
        "components_containing_towns": len(sorted_comps),
        "graph_components_including_empty": n_graph_components,
        "smaller_components_with_towns": len(rows),
        "components_without_road_vertices": sum(row["road_nodes"] == 0 for row in rows),
        "nearest_main_road_vertex_gap_under_10m": sum(v <= 0.01 for v in nearest_by_comp.values()),
        "nearest_main_road_vertex_gap_under_100m": sum(v <= 0.1 for v in nearest_by_comp.values()),
        "nearest_main_road_vertex_gap_under_1km": sum(v <= 1 for v in nearest_by_comp.values()),
        "interpretation": (
            "Distance measures nearest EXISTING ROAD VERTICES; it is not the exact distance "
            "between line segments and is not proof of an at-grade connection. "
            "Small gaps require inspection of OSM node IDs, road classes, grade separation, "
            "bridges and physical route legality. No routing edges were changed."
        ),
        "source_filter_caution": (
            "Current transport builder includes trunk, primary, secondary, tertiary only "
            "unless optional classes are explicitly enabled; service, residential, "
            "unclassified, link roads, and motorways can be missing."
        ),
    }
    return summary, rows, town_rows, {"type": "FeatureCollection", "features": geojson_features}


def _verify_saved_graph(workspace: Path) -> None:
    """Use the same byte-level input-provenance checks as connectivity stage."""
    import config as C
    import run as runner
    C.WORK = workspace / "work"
    C.DATA = workspace / "data"
    runner.DATA_EXPLICIT = False
    runner._require_stage1_current()


def run(*, workspace: Path, verify: bool = True, top: int = 20) -> int:
    workspace = Path(workspace).expanduser().resolve()
    graph_path = workspace / "work" / "graph_full.npz"
    source_path = workspace / "work" / "sources.npz"
    if top < 1:
        raise ValueError("--top must be a positive integer")
    if not graph_path.is_file() or not source_path.is_file():
        raise FileNotFoundError("Saved national stage-1 graph not found. Run graph/connectivity first.")
    if verify:
        _verify_saved_graph(workspace)
    print("READ ONLY: Loading saved graph. No download, re-extraction, re-snap or new edges.", flush=True)
    with np.load(graph_path, allow_pickle=False) as a, np.load(source_path, allow_pickle=False) as b:
        graph = {name: a[name] for name in ("node_lonlat", "flags", "u", "v", "code")}
        src = {name: b[name] for name in ("node_idx", "tier", "offset", "ids", "names", "lonlat")}
        source_manifest = json.loads(str(b["input_manifest"])) if "input_manifest" in b.files else {}
    summary, comps, towns, geo = analyze_graph(graph, src)
    summary["graph_path"] = str(graph_path)
    summary["source_path"] = str(source_path)
    summary["source_fingerprint"] = source_manifest.get("fingerprint")
    summary["provenance_checked"] = bool(verify)
    out = workspace / "output" / "connectivity_diagnostics"
    out.mkdir(parents=True, exist_ok=True)
    comp_columns = ("component", "town_count", "town_names", "component_nodes", "road_nodes", "road_edges", "ferry_edges",
                    "gangway_edges", "nearest_main_road_vertex_km", "from_node", "to_main_node", "from_lon", "from_lat", "to_lon", "to_lat")
    town_columns = ("psgc_code", "name", "state", "component", "actual_snap_tier", "actual_snap_offset_km",
                    "origin_lon", "origin_lat", "nearest_main_road_vertex_km", "nearest_main_road_node",
                    "nearest_main_road_lon", "nearest_main_road_lat")
    _write_csv(out / "component_gaps.csv", comp_columns, comps)
    _write_csv(out / "affected_towns.csv", town_columns, towns)
    for name, value in (("component_gap_lines.geojson", geo), ("diagnostic_summary.json", summary)):
        path = out / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    print(f"Main component: {summary['main_component_towns']:,} towns; remaining: {summary['smaller_components_with_towns']} components / {len(towns)} affected towns", flush=True)
    print(f"Nearest main road-vertex gaps: <=10 m: {summary['nearest_main_road_vertex_gap_under_10m']}; <=100 m: {summary['nearest_main_road_vertex_gap_under_100m']}; <=1 km: {summary['nearest_main_road_vertex_gap_under_1km']}", flush=True)
    print("Closest disconnected component pairs (not validated transport links):", flush=True)
    for row in comps[:top]:
        print(f"  {row['component']:5} {str(row['nearest_main_road_vertex_km']):>10} km  {row['town_names'][:105]}", flush=True)
    print(f"Reports: {out}", flush=True)
    return 0
