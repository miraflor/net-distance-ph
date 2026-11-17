"""Read-only local OSM node-ID examination around connectivity component gaps.

This does NOT repair topology, create edges, modify routing sources or certify
legal access. Positive paths are OSM node-ID paths in a bounded local window.
Negative findings are explicitly inconclusive beyond that window.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

from scripts.build_transport import public_motorcar_access

# Nondefault road classes worth testing. The current filter is loaded from the
# selected TRANSPORT RELEASE, not hard-coded here.
ORDINARY_EXTRA = frozenset({
    "motorway", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link", "unclassified", "residential",
    "living_street",
})
SERVICE_EXTRA = frozenset({"service"})
RESTRICTED = {"no", "private"}
ENDPOINT_ROUNDING = 1_000_000  # matches the graph's default 6-decimal noder


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _confined(base: Path, candidate: str) -> Path:
    path = Path(candidate)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Untrusted release file path: {candidate}")
    result = (base / path).resolve()
    result.relative_to(base.resolve())
    if not result.is_file():
        raise FileNotFoundError(result)
    return result


def _read_gap_csv(path: Path, top: int) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fp:
        rows = list(csv.DictReader(fp))
    needed = {"component", "town_names", "nearest_main_road_vertex_km", "from_lon", "from_lat", "to_lon", "to_lat"}
    if not rows or not needed.issubset(rows[0]):
        raise ValueError("component_gaps.csv is missing required fields")
    valid = []
    for r in rows:
        try:
            v = float(r["nearest_main_road_vertex_km"])
            r["a"] = (float(r["from_lon"]), float(r["from_lat"]))
            r["b"] = (float(r["to_lon"]), float(r["to_lat"]))
        except (ValueError, TypeError):
            continue  # no road vertices in a component
        if not math.isfinite(v) or v < 0 or not all(math.isfinite(z) for p in (r["a"], r["b"]) for z in p):
            raise ValueError(f"Invalid component gap coordinate or metric: {r['component']}")
        r["km"] = v
        valid.append(r)
    valid.sort(key=lambda row: (row["km"], row["component"]))
    if len({r["component"] for r in valid}) != len(valid):
        raise ValueError("Duplicate component IDs in gap report")
    # Check only the gaps requested for examination. Faraway components must
    # not prevent a --top N inspection of local road-network breaks.
    selected = valid[:top]
    for row in selected:
        # WGS84 validity is global: Kalayaan (Pag-asa Island, ~114.3 E) is
        # within the Philippines' locality roster but west of an old hardcoded
        # [115, 128] longitude envelope. No arbitrary country bounding box.
        if not all(-180 <= p[0] <= 180 and -90 <= p[1] <= 90
                   for p in (row["a"], row["b"])):
            raise ValueError(f"Invalid WGS84 longitude/latitude in gap: {row['component']}")
    return selected


def _metres(p, q) -> float:
    """Local equirectangular geometry: candidate screening, not routing lengths."""
    lat = math.radians((p[1] + q[1]) * 0.5)
    dx = (p[0] - q[0]) * 111_319.49 * math.cos(lat)
    dy = (p[1] - q[1]) * 110_946.0
    return math.hypot(dx, dy)


def _point_segment_m(p, a, b) -> float:
    lat = math.radians(p[1])
    factor = 111_319.49 * math.cos(lat)
    x, y = (a[0] - p[0]) * factor, (a[1] - p[1]) * 110_946.0
    vx, vy = (b[0] - a[0]) * factor, (b[1] - a[1]) * 110_946.0
    denom = vx * vx + vy * vy
    t = min(1.0, max(0.0, -(x * vx + y * vy) / denom)) if denom else 0.0
    return math.hypot(x + t * vx, y + t * vy)


def _to_ends(p, points) -> float:
    if not points:
        return float("inf")
    if len(points) == 1:
        return _metres(p, points[0][1:])
    return min(_point_segment_m(p, a[1:], b[1:]) for a, b in zip(points[:-1], points[1:]))


def _rounded_point(p) -> tuple[int, int]:
    # Important: identical to node_coordinates() in graphbuild.py.
    return (int(round(float(p[0]) * ENDPOINT_ROUNDING)),
            int(round(float(p[1]) * ENDPOINT_ROUNDING)))


def _is_restricted(tags: dict) -> bool:
    """Exactly match the transport builder's most-specific vehicle access rule."""
    return not public_motorcar_access(tags)


def _is_public_extra(tags: dict) -> bool:
    return not _is_restricted(tags) and tags.get("area") != "yes" and tags.get("route") != "ferry"


def _in_window(node, row, radius_m):
    return min(_metres(node[1:], row["a"]), _metres(node[1:], row["b"])) <= radius_m


def select_way_for_gap(nodes: list[tuple[int, float, float]], row: dict, radius_m: float) -> float | None:
    """Minimum geometric distance to either endpoint; None if outside window."""
    if len(nodes) < 2:
        return None
    xy = [(n[1], n[2]) for n in nodes]
    # A cheap bounding-box pretest keeps the full-country pass affordable.
    margin_lat = radius_m / 110_000 + 0.0002
    low_lat = min(row["a"][1], row["b"][1]) - margin_lat
    high_lat = max(row["a"][1], row["b"][1]) + margin_lat
    margin_lon = radius_m / (111_000 * max(0.2, math.cos(math.radians(row["a"][1])))) + 0.0002
    low_lon = min(row["a"][0], row["b"][0]) - margin_lon
    high_lon = max(row["a"][0], row["b"][0]) + margin_lon
    if max(x[0] for x in xy) < low_lon or min(x[0] for x in xy) > high_lon or max(x[1] for x in xy) < low_lat or min(x[1] for x in xy) > high_lat:
        return None
    d = min(_to_ends(row["a"], nodes), _to_ends(row["b"], nodes))
    return round(d, 4) if d <= radius_m else None


def extract_nearby_ways(source: Path, gaps: list[dict], radius_m: float) -> tuple[dict[str, list[dict]], dict]:
    """Exactly one pyosmium locations-enabled scan of a cached local PBF."""
    try:
        import osmium
    except ImportError as exc:
        raise ImportError('PyOsmium is required: python -m pip install -e ".[transport]"') from exc
    found = {row["component"]: [] for row in gaps}
    location_errors = (getattr(osmium, "InvalidLocationError", ValueError),
                       ValueError, RuntimeError, AttributeError)

    class Handler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.ways_seen = 0
            self.eligible_seen = 0
            self.skipped_missing_locations = 0
            self.last_progress = time.monotonic()

        def way(self, way):
            self.ways_seen += 1
            if self.ways_seen % 500_000 == 0:
                print(f"  scanned {self.ways_seen:,} OSM ways; inspected {self.eligible_seen:,} highway/ferry ways", flush=True)
            tags = {str(item.k): str(item.v) for item in way.tags}
            highway = tags.get("highway", "")
            ferry = tags.get("route") == "ferry"
            if not highway and not ferry:
                return
            self.eligible_seen += 1
            try:
                nodes = [(int(n.ref), float(n.location.lon), float(n.location.lat)) for n in way.nodes]
            except location_errors:
                self.skipped_missing_locations += 1
                return
            if len(nodes) < 2:
                return
            for row in gaps:
                d = select_way_for_gap(nodes, row, radius_m)
                if d is None:
                    continue
                found[row["component"]].append({
                    "way_id": int(way.id), "highway": highway if not ferry else "ferry_route",
                    # Preserve absent tags as absent. The builder consults the first
                    # PRESENT access tag by specificity; an injected motorcar=""
                    # would mask a real, less-specific access=private restriction.
                    "tags": {k: tags[k] for k in
                             ("access", "vehicle", "motor_vehicle", "motorcar", "oneway", "bridge", "tunnel", "layer", "service", "area", "route") if k in tags},
                    "nodes": nodes, "near_gap_m": d,
                })
    h = Handler()
    h.apply_file(str(source), locations=True, idx="flex_mem")
    return found, {"osm_ways_scanned": h.ways_seen, "highway_or_ferry_ways_seen": h.eligible_seen,
                   "selected_ways_missing_locations": h.skipped_missing_locations}


def _path(ways: list[dict], row: dict, radius_m: float, baseline: set[str], mode: str):
    """BFS over EXACT shared OSM node IDs, never coordinate-only junctions."""
    added = ORDINARY_EXTRA if mode != "baseline" else frozenset()
    if mode == "with_service":
        added = added | SERVICE_EXTRA
    allowable = baseline | added
    adj = defaultdict(list)
    sources, targets = set(), set()
    from_xy = _rounded_point(row["a"])
    to_xy = _rounded_point(row["b"])
    for way in ways:
        highway = way["highway"]
        if highway not in allowable or way["tags"].get("route") == "ferry":
            continue
        # A selected highway class is NOT proof that the OSM way was extracted:
        # the transport builder also rejects explicit private/prohibited motor access.
        # This applies to ALL classes, not only hypothetical extra ones.
        if not public_motorcar_access(way["tags"]):
            continue
        if highway not in baseline and not _is_public_extra(way["tags"]):
            continue
        points = way["nodes"]
        if highway in baseline:
            for n in points:
                if _rounded_point(n[1:]) == from_xy:
                    sources.add(n[0])
                if _rounded_point(n[1:]) == to_xy:
                    targets.add(n[0])
        for a, b in zip(points[:-1], points[1:]):
            if a[0] == b[0] or not (_in_window(a, row, radius_m) and _in_window(b, row, radius_m)):
                continue
            record = (b[0], way["way_id"], highway, way["tags"], b[1:])
            reverse = (a[0], way["way_id"], highway, way["tags"], a[1:])
            adj[a[0]].append(record)
            adj[b[0]].append(reverse)
    if not sources or not targets:
        return {"matched_from_count": len(sources), "matched_to_count": len(targets), "found": None,
                "path_way_ids": [], "path_road_classes": [], "path_added_classes": [], "path_nodes": []}
    # BFS for minimum-hop witness, not shortest travel distance.
    todo = deque(sorted(sources))
    parent = {s: None for s in sources}
    reached = next((s for s in sources if s in targets), None)
    while todo and reached is None:
        cur = todo.popleft()
        for dest, way_id, road_class, tags, point in adj.get(cur, ()):
            if dest not in parent:
                parent[dest] = (cur, way_id, road_class, tags, point)
                if dest in targets:
                    reached = dest
                    break
                todo.append(dest)
    if reached is None:
        return {"matched_from_count": len(sources), "matched_to_count": len(targets), "found": False,
                "path_way_ids": [], "path_road_classes": [], "path_added_classes": [], "path_nodes": []}
    trace = []
    cur = reached
    while parent[cur] is not None:
        previous, way_id, road_class, tags, point = parent[cur]
        trace.append((way_id, road_class, tags, point))
        cur = previous
    trace.reverse()
    ways_in_path = list(dict.fromkeys(str(x[0]) for x in trace))
    classes_in_path = sorted({x[1] for x in trace})
    return {"matched_from_count": len(sources), "matched_to_count": len(targets), "found": True,
            "path_way_ids": ways_in_path,
            "path_road_classes": classes_in_path,
            "path_added_classes": sorted(set(classes_in_path) - baseline),
            "path_nodes": [row["a"], *[step[3] for step in trace]],
            "path_restricted_access": any(_is_restricted(step[2]) for step in trace),
            "path_oneway_tags": any(step[2].get("oneway") not in ("", "no") for step in trace),
            "path_bridge_tunnel_tags": any(step[2].get("bridge") or step[2].get("tunnel") for step in trace),
            "path_access_caveat": any(step[2].get("access") in ("destination", "permissive", "customers") for step in trace),
            "path_hops": len(trace)}


def examine(gaps: list[dict], nearby: dict[str, list[dict]], baseline: set[str], radius_m: float):
    """Deterministic pure examination function for offline tests and report generation."""
    summary, way_rows, features = [], [], []
    for row in gaps:
        name = row["component"]
        ways = nearby.get(name, [])
        base = _path(ways, row, radius_m, baseline, "baseline")
        broad = _path(ways, row, radius_m, baseline, "expanded")
        service = _path(ways, row, radius_m, baseline, "with_service")
        if base["found"] is True:
            verdict = "BASELINE_OSM_NODE_PATH_NEEDS_GRAPH_REVIEW"
        elif broad["found"] is True:
            verdict = "EXPANDED_ORDINARY_ROADS_RECOVER_LOCAL_PATH"
        elif service["found"] is True:
            verdict = "SERVICE_ROADS_RECOVER_LOCAL_PATH_ACCESS_REVIEW"
        elif base["found"] is None:
            verdict = "OSM_ENDPOINT_NOT_MATCHED_TO_BASELINE_ROAD"
        else:
            verdict = "NO_LOCAL_OSM_NODE_PATH_IN_TESTED_RADIUS"
        extra = [x for x in ways if x["highway"] not in baseline and x["highway"] != "ferry_route"]
        nearest = min(extra, key=lambda x: (x["near_gap_m"], x["way_id"])) if extra else None
        obj = {
            "component": name, "town_names": row["town_names"],
            "graph_vertex_gap_m": round(1000 * row["km"], 3),
            "osm_way_count_within_radius": len(ways),
            "baseline_endpoint_from_matches": base["matched_from_count"],
            "baseline_endpoint_to_matches": base["matched_to_count"],
            "baseline_local_path": base["found"],
            "expanded_ordinary_local_path": broad["found"],
            "expanded_plus_service_local_path": service["found"],
            "verdict": verdict,
            "nearest_excluded_way_id": nearest["way_id"] if nearest else "",
            "nearest_excluded_road_class": nearest["highway"] if nearest else "",
            "nearest_excluded_way_m": nearest["near_gap_m"] if nearest else "",
            "witness_added_classes": ";".join((broad if broad["found"] is True else service)["path_added_classes"]),
            "witness_way_ids": ";".join((broad if broad["found"] is True else service)["path_way_ids"]),
            "witness_oneway_tags": bool((broad if broad["found"] is True else service).get("path_oneway_tags")),
            "witness_access_caveat": bool((broad if broad["found"] is True else service).get("path_access_caveat")),
            "witness_bridge_or_tunnel": bool((broad if broad["found"] is True else service).get("path_bridge_tunnel_tags")),
        }
        summary.append(obj)
        for w in sorted(ways, key=lambda z: (z["near_gap_m"], z["way_id"])):
            if w["highway"] in baseline:
                group = "included" if public_motorcar_access(w["tags"]) else "excluded_access"
            elif w["highway"] in ORDINARY_EXTRA:
                group = "ordinary_extra" if _is_public_extra(w["tags"]) else "restricted_or_area"
            elif w["highway"] in SERVICE_EXTRA:
                group = "service_extra" if _is_public_extra(w["tags"]) else "restricted_or_area"
            else:
                group = "other_or_nonmotorized"
            way_rows.append({"component": name, "osm_way_id": w["way_id"], "highway": w["highway"],
                             "scenario": group, "distance_to_gap_endpoint_m": w["near_gap_m"],
                             **{k: w["tags"].get(k, "") for k in ("access", "vehicle", "motor_vehicle", "motorcar", "oneway", "bridge", "tunnel", "layer", "service")}})
        features.append({"type": "Feature", "properties": {"component": name, "kind": "gap_between_separate_graph_vertices",
                "road_gap_m": obj["graph_vertex_gap_m"], "verdict": verdict, "never_add_as_connection": True},
                "geometry": {"type": "LineString", "coordinates": [list(row["a"]), list(row["b"])]}})
        witness = broad if broad["found"] is True else service
        if witness["found"] is True and len(witness["path_nodes"]) >= 2:
            features.append({"type": "Feature", "properties": {"component": name, "kind": "local_osm_node_id_witness",
                "classes_added": witness["path_added_classes"],
                "interpretation": "Undirected local OSM node-ID path, not proof of drivable legal junction or ferry service"},
                "geometry": {"type": "LineString", "coordinates": [list(p) for p in witness["path_nodes"]]}})
    return summary, way_rows, {"type": "FeatureCollection", "features": features}


def _csv(path: Path, rows: list[dict]):
    if not rows:
        raise ValueError("No data to publish")
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _json(path: Path, obj):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def run(*, workspace: Path, top: int = 9, radius_m: float = 1000.0) -> int:
    import numpy as np
    workspace = Path(workspace).expanduser().resolve()
    if not (1 <= top <= 500):
        raise ValueError("--top must be between 1 and 500")
    if not (100 <= radius_m <= 10_000) or not math.isfinite(radius_m):
        raise ValueError("--radius-m must be between 100 and 10,000 metres")
    data = workspace / "data"
    input_dir = workspace / "output" / "connectivity_diagnostics"
    from .connectivity_diagnostics import _verify_saved_graph
    _verify_saved_graph(workspace)
    diag = json.loads((input_dir / "diagnostic_summary.json").read_text(encoding="utf-8"))
    if diag.get("status") != "DIAGNOSTIC_ONLY_NO_GRAPH_EDITS" or not diag.get("provenance_checked"):
        raise ValueError("Prior gap report is missing or was not provenance-checked; rerun diagnose-connectivity")
    if diag.get("graph_path") != str((workspace / "work" / "graph_full.npz").resolve()):
        raise ValueError("Gap report does not belong to this workspace graph")
    with np.load(workspace / "work" / "sources.npz", allow_pickle=False) as z:
        actual = json.loads(str(z["input_manifest"]))["fingerprint"]
    if actual != diag.get("source_fingerprint"):
        raise ValueError("Gap diagnosis source fingerprint is stale; rerun diagnose-connectivity")
    gaps = _read_gap_csv(input_dir / "component_gaps.csv", top)
    if not gaps:
        raise ValueError("No usable component gaps to inspect")

    selected = json.loads((data / "current_transport.json").read_text(encoding="utf-8"))
    prov = json.loads(_confined(data, selected["provenance"]).read_text(encoding="utf-8"))
    baseline = set(prov.get("road_filter", []))
    if not baseline or not all(isinstance(x, str) for x in baseline):
        raise ValueError("Selected transport provenance has no valid road class filter")
    if prov.get("source_sha256") != selected.get("source_sha256"):
        raise ValueError("Transport release pointer and provenance source hashes disagree")
    source = Path(prov["source"]).expanduser().resolve()
    if source.suffix.lower() != ".pbf" or not source.is_file():
        raise FileNotFoundError(f"Selected transport's cached PBF is missing: {source}")
    # Reject a cache modified since the selected graph source was produced.
    print(f"VERIFY: reading cached PBF hash (no download): {source}", flush=True)
    source_sha = _sha256(source)
    if source_sha != prov["source_sha256"]:
        raise ValueError("Cached PBF SHA-256 differs from the transport release; refusing investigation")
    print(f"SCAN: original PBF once; {len(gaps)} local gaps, {radius_m:g} m from either endpoint", flush=True)
    nearby, counts = extract_nearby_ways(source, gaps, radius_m)
    observations, rows, geojson = examine(gaps, nearby, baseline, radius_m)
    # Presence of a path is a topology observation in a radius-limited local
    # subgraph, never an authorized graph edit or a feasibility certificate.
    status = Counter(r["verdict"] for r in observations)
    report = {"schema": "net-distance-ph/osm-gap-inspection/v1",
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "status": "READ_ONLY_OSM_NODE_ID_LOCAL_EXAMINATION",
              "source_pbf": str(source), "source_pbf_sha256": source_sha,
              "source_fingerprint": actual, "boundary_level": workspace.name if workspace.parent.name == "levels" else "town",
              "road_filter_from_selected_release": sorted(baseline),
              "extra_classes_ordinary_tested": sorted(ORDINARY_EXTRA - baseline),
              "extra_classes_service_tested": sorted(SERVICE_EXTRA - baseline),
              "radius_m": radius_m, "gaps_inspected": len(gaps),
              "scanning_counts": counts, "verdict_counts": dict(status),
              "warnings": ["Paths use shared OSM node IDs and a LOCAL radius, and treat edges as undirected.",
                           "All candidate OSM road ways use the same public motorcar-access rule as the selected transport builder; class membership alone is insufficient.",
                           "An observed OSM path does not prove legal vehicle access, ferry service, or valid elevation topology.",
                           "No path is inconclusive: a connecting route may run outside the radius.",
                           "Endpoint match requires graph rounding at six decimal places; unmatched endpoints are not evidence of no road.",
                           "Nearby ways or close vertices alone never establish connectivity.",
                           "No data acquisition, source shapefiles, graph files, or graph thresholds were changed."]}
    out = workspace / "output" / "osm_gap_inspection"
    out.mkdir(parents=True, exist_ok=True)
    _csv(out / "gap_findings.csv", observations)
    if rows:
        _csv(out / "nearby_osm_ways.csv", rows)
    else:
        (out / "nearby_osm_ways.csv").write_text("component,osm_way_id,highway,scenario,distance_to_gap_endpoint_m,access,vehicle,motor_vehicle,motorcar,oneway,bridge,tunnel,layer,service\n", encoding="utf-8")
    _json(out / "local_paths.geojson", geojson)
    _json(out / "inspection_summary.json", report)
    for o in observations:
        print(f"{o['component']}: {o['graph_vertex_gap_m']:7.2f} m; "
              f"OSM ways={o['osm_way_count_within_radius']:4}; {o['verdict']}; "
              f"added={o['witness_added_classes'] or '-'}", flush=True)
    print(f"REPORTS: {out}", flush=True)
    return 0
