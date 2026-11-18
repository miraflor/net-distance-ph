"""Read-only OSM node-ID inspection of v50 population-network gap candidates.

Reuses the established local OSM gap inspector. No new edges, model parameter
changes, downloads, routing jobs, or modifications to existing outputs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
import math
from pathlib import Path


def _read_csv(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"Empty report CSV: {path}")
    return rows


def select_gap_candidates(priorities: list[dict], candidates: list[dict],
                          *, top: int = 15, ranks: tuple[int, ...] = (),
                          relation: str = "both") -> tuple[list[dict], list[dict]]:
    """Deterministic v50 candidate selection; avoids duplicate main/other endpoints.

    Only rows from the *already reported* v50 geometric gap search are used.
    This function performs no approximation, graph edits, or PBF access.
    """
    if not isinstance(top, int) or not 1 <= top <= 229:
        raise ValueError("--top must be an integer in [1, 229]")
    if relation not in {"main", "other", "both"}:
        raise ValueError("--relation must be main, other, or both")
    allowed = set(ranks) if ranks else set(range(1, top + 1))
    if any(type(x) is not int or x < 1 or x > 229 for x in allowed):
        raise ValueError("--ranks must contain ranks in [1, 229]")
    priorities_by_rank = {}
    for p in priorities:
        rank = int(p["rank"])
        if rank in priorities_by_rank:
            raise ValueError(f"Duplicate priority rank: {rank}")
        priorities_by_rank[rank] = p
    if ranks and set(ranks) - priorities_by_rank.keys():
        raise ValueError("Selected rank is absent from the v50 priority-components report")
    found, skipped, known = [], [], set()
    for c in sorted(candidates, key=lambda x: (int(x["rank"]),
                                                0 if x["relation"] == "nearest_national_main_component" else 1)):
        rank = int(c["rank"])
        if rank not in allowed:
            continue
        if rank not in priorities_by_rank:
            raise ValueError(f"Candidate rank {rank} absent from priorities")
        typ = c["relation"]
        if typ not in {"nearest_national_main_component", "nearest_any_other_component"}:
            raise ValueError(f"Unexpected relation: {typ}")
        if relation == "main" and typ != "nearest_national_main_component":
            continue
        if relation == "other" and typ != "nearest_any_other_component":
            continue
        if c["status"] != "CANDIDATE_WITHIN_RADIUS":
            skipped.append({"rank": rank, "towns": priorities_by_rank[rank]["towns"],
                            "relation": typ, "reason": c["status"]})
            continue
        coords = (float(c["source_lon"]), float(c["source_lat"]),
                  float(c["candidate_lon"]), float(c["candidate_lat"]))
        dist = float(c["geometric_gap_km"])
        if not (math.isfinite(dist) and dist >= 0 and all(math.isfinite(v) for v in coords)):
            raise ValueError(f"Nonfinite geometric gap for rank {rank}")
        if not (all(-180 <= coords[i] <= 180 for i in (0, 2)) and
                all(-90 <= coords[i] <= 90 for i in (1, 3))):
            raise ValueError(f"Invalid geographic coordinates for rank {rank}")
        key = (rank, str(c["source_node"]), str(c["candidate_node"]))
        if key in known:
            continue  # main and nearest-other can identify the exact same pair
        known.add(key)
        tag = "main" if typ == "nearest_national_main_component" else "other"
        found.append({"component": f"rank{rank:02d}_{tag}",
                      "priority_rank": rank,
                      "core_component": int(c["core_component"]),
                      "original_component": int(c["original_component"]),
                      "source_node": int(float(c["source_node"])),
                      "candidate_node": int(float(c["candidate_node"])),
                      "source_node_kind": c.get("source_node_kind", ""),
                      "candidate_node_kind": c.get("candidate_node_kind", ""),
                      "modeled_population": int(c["modeled_population"]),
                      "town_names": priorities_by_rank[rank]["towns"],
                      "relation": typ, "a": coords[:2], "b": coords[2:], "km": dist})
    if not found:
        raise ValueError("No geometric gap candidates matched this selection; rerun audit-topology with a larger search radius or select other ranks")
    if len(found) > 100:
        raise ValueError("Refusing more than 100 gap windows in one PBF scan")
    return found, skipped


def endpoint_node_identities(gaps: list[dict], nearby: dict[str, list[dict]]):
    """List OSM node identities at exact graph-rounded endpoints.

    Different OSM IDs at the same rounded coordinate are preserved, not joined.
    If no IDs match, the local path inspector's result is inconclusive.
    """
    from . import osm_gap_inspection as osm
    rows = []
    for gap in gaps:
        for side, coord in (("source", gap["a"]), ("candidate", gap["b"])):
            key = osm._rounded_point(coord)
            seen = set()
            for way in nearby.get(gap["component"], []):
                for n in way["nodes"]:
                    if osm._rounded_point(n[1:]) != key:
                        continue
                    pair = (int(n[0]), int(way["way_id"]))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    rows.append({"gap_id": gap["component"], "side": side,
                                 "graph_node": gap["source_node" if side == "source" else "candidate_node"],
                                 "osm_node_id": pair[0], "osm_way_id": pair[1],
                                 "highway": way["highway"],
                                 "access": way["tags"].get("access", ""),
                                 "bridge": way["tags"].get("bridge", ""),
                                 "tunnel": way["tags"].get("tunnel", ""),
                                 "layer": way["tags"].get("layer", "")})
    return rows


def _write_csv(path: Path, rows: list[dict], columns: list[str]):
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _validate_gap_coordinates(graph: Path, gaps: list[dict]) -> None:
    """Ensure the candidate coordinates really identify saved original vertices."""
    import numpy as np
    with np.load(graph, allow_pickle=False) as archive:
        coords = archive["node_lonlat"]
        for gap in gaps:
            for side, node_key, location_key in (("source", "source_node", "a"),
                                                 ("candidate", "candidate_node", "b")):
                node = gap[node_key]
                if not 0 <= node < len(coords):
                    raise ValueError(f"Invalid {side} graph node index: {node}")
                if not np.allclose(coords[node], gap[location_key], rtol=0, atol=1e-6):
                    raise ValueError(f"v50 {gap['component']} {side} coordinates differ from current graph")


def execute(args) -> int:
    import numpy as np
    from . import osm_gap_inspection as osm
    from .connectivity_diagnostics import _verify_saved_graph
    from .workspace import timed_step, log_prefix

    workspace = Path(args.workspace or (Path.home() / "Documents" / "net-distance-ph")).expanduser().resolve()
    if not math.isfinite(args.radius_m) or not 100 <= args.radius_m <= 10_000:
        raise ValueError("--radius-m must be between 100 and 10000 metres")
    if args.ranks and len(set(args.ranks)) != len(args.ranks):
        raise ValueError("Duplicate --ranks supplied")
    latest_path = workspace / "variants" / "population_weighted" / "output" / "latest_run.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    routing_hash = latest["routing_hash"]
    directory = workspace / "output" / "topology_diagnostics" / routing_hash[:16]
    diag = json.loads((directory / "topology_diagnostics.json").read_text(encoding="utf-8"))
    if (diag.get("routing_hash") != routing_hash or
            diag.get("geometric_gap_status") != "COMPLETED" or
            diag.get("lineage", {}).get("preserved_for_selected_representatives") is not True):
        raise ValueError("v50 topology report is stale or incomplete; run audit-topology first")
    priority = _read_csv(directory / "priority_components.csv")
    candidates = _read_csv(directory / "candidate_geometric_gaps.csv")
    gaps, skipped = select_gap_candidates(priority, candidates, top=args.top,
                                          ranks=tuple(args.ranks or ()), relation=args.relation)
    print(log_prefix() + f" SELECTED: {len(gaps)} geometric gaps; {len(skipped)} no-candidate records", flush=True)

    graph = workspace / "work" / "graph_full.npz"
    with timed_step("verify graph, routing, and selected OSM source provenance"):
        if osm._sha256(graph) != diag["stage1_graph_sha256"]:
            raise ValueError("Current original graph differs from the audited v50 graph")
        with np.load(workspace / "work" / "sources.npz", allow_pickle=False) as z:
            fingerprint = json.loads(str(z["input_manifest"]))["fingerprint"]
        if fingerprint != diag["stage1_source_fingerprint"]:
            raise ValueError("Current stage-1 source fingerprint differs from v50")
        _verify_saved_graph(workspace)
        _validate_gap_coordinates(graph, gaps)
        data = workspace / "data"
        selected = json.loads((data / "current_transport.json").read_text(encoding="utf-8"))
        prov = json.loads(osm._confined(data, selected["provenance"]).read_text(encoding="utf-8"))
        road_filter = prov.get("road_filter", [])
        if not road_filter or not all(isinstance(c, str) for c in road_filter):
            raise ValueError("Selected transport has no valid road class filter")
        if prov.get("source_sha256") != selected.get("source_sha256"):
            raise ValueError("Transport provenance and selected release source hashes differ")
        pbf = Path(prov["source"]).expanduser().resolve()
        if not pbf.is_file() or pbf.suffix.lower() != ".pbf":
            raise FileNotFoundError(f"Selected release's cached OSM PBF missing: {pbf}")
        pbf_sha = osm._sha256(pbf)
        if pbf_sha != prov["source_sha256"]:
            raise ValueError("Cached OSM PBF has changed since transport extraction")
    with timed_step("scan original OSM ways with node identity near selected gaps"):
        nearby, scan_counts = osm.extract_nearby_ways(pbf, gaps, float(args.radius_m))
    with timed_step("analyze exact OSM node-ID local paths and tags"):
        findings, ways, geojson = osm.examine(gaps, nearby, set(road_filter), float(args.radius_m))
        identities = endpoint_node_identities(gaps, nearby)
    summary = {"schema": "net-distance-ph/topology-osm-investigation/v51",
               "generated_at": datetime.now().isoformat(timespec="seconds"),
               "status": "LOCAL_OSM_ID_EVIDENCE_ONLY",
               "routing_hash": routing_hash,
               "original_graph_sha256": diag["stage1_graph_sha256"],
               "source_fingerprint": fingerprint, "pbf_sha256": pbf_sha,
               "road_filter_from_selected_transport": sorted(set(road_filter)),
               "search_radius_m": args.radius_m,
               "candidate_gap_count": len(gaps), "noncandidate_records": skipped,
               "osm_scan": scan_counts,
               "verdicts": dict(Counter(r["verdict"] for r in findings)),
               "warnings": ["No edges were created, modified, or recommended automatically.",
                            "Local OSM shared-node paths are undirected; not proof of vehicle accessibility or bridge/ferry service.",
                            "Nearby ways without a valid OSM node-ID path do not prove disconnection outside the search window.",
                            "Identical rounded coordinates with distinct OSM node IDs do not establish a legitimate junction.",
                            "Ferry routes are inventoried but are NOT included in the road-only OSM node-ID BFS."]}
    out = workspace / "output" / "topology_osm_investigation" / routing_hash[:16]
    out.mkdir(parents=True, exist_ok=True)
    cols = list(findings[0])
    _write_csv(out / "gap_findings.csv", findings, cols)
    waycols = ["component", "osm_way_id", "highway", "scenario", "distance_to_gap_endpoint_m",
               "access", "vehicle", "motor_vehicle", "motorcar", "oneway", "bridge", "tunnel", "layer", "service"]
    _write_csv(out / "nearby_osm_ways.csv", ways, waycols)
    idcols = ["gap_id", "side", "graph_node", "osm_node_id", "osm_way_id", "highway", "access", "bridge", "tunnel", "layer"]
    _write_csv(out / "endpoint_osm_node_ids.csv", identities, idcols)
    osm._json(out / "local_osm_paths.geojson", geojson)
    osm._json(out / "investigation_summary.json", summary)
    for row in findings:
        print(log_prefix() + f" {row['component']}: {row['graph_vertex_gap_m']:.1f} m; "
              f"{row['verdict']} | OSM ways={row['osm_way_count_within_radius']} | "
              f"extra classes={row['witness_added_classes'] or '-'}", flush=True)
    print(log_prefix() + f" OSM INVESTIGATION: {out}", flush=True)
    return 0
