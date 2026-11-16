#!/usr/bin/env python3
"""First-class nationwide locality connectivity audit for net-distance-ph.

Run only after `python run.py graph` successfully publishes
`work/graph_full.npz` and `work/sources.npz` in the same workspace.

Does not build a graph, download data, reduce, or compute all-pairs distances.
It evaluates connected components and exact official PSGC locality coverage.

Run as `python run.py connectivity` after the graph stage, or directly as
`net-distance-ph-connectivity`. The `--self-test` mode uses synthetic data only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from itertools import combinations
import heapq
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

ROAD = 0
FERRY = 1
GANGWAY = 2


def choose2(n: int) -> int:
    return int(n) * (int(n) - 1) // 2


def labels_for_graph(n_nodes, u, v):
    """Undirected connected components using one stored direction per edge.

    The topology-only adjacency does not need duplicate reverse edges or the
    (potentially huge) floating-point edge-weight array.
    """
    n_nodes = int(n_nodes)
    if n_nodes < 1:
        raise ValueError("The graph has no nodes")
    if u.shape != v.shape or u.ndim != 1:
        raise ValueError("Edge endpoint arrays have inconsistent shapes")
    if u.size and (np.min(u) < 0 or np.min(v) < 0 or np.max(u) >= n_nodes or np.max(v) >= n_nodes):
        raise ValueError("An edge references a node outside the graph")
    if u.size:
        # Boolean values prevent integer overflow when edges are duplicated.
        a = coo_matrix((np.ones(u.size, dtype=np.bool_), (u, v)), shape=(n_nodes, n_nodes)).tocsr()
    else:
        a = coo_matrix((n_nodes, n_nodes), dtype=np.bool_).tocsr()
    count, labels = connected_components(a, directed=False, return_labels=True)
    return int(count), labels


def audit(roster, ids, names, node_idx, tier, offset, coords, u, v, code, flags):
    """Compute results from net-distance-ph stage-1 arrays, without I/O."""
    official_codes = [str(x["psgc_code"]) for x in roster]
    if not official_codes or len(official_codes) != len(set(official_codes)):
        raise ValueError("Official roster is empty or has duplicate PSGC codes")
    if any(not re.fullmatch(r"\d{10}", x) for x in official_codes):
        raise ValueError("Official PSGC codes must be ten-digit text values")

    ids = [str(x) for x in ids]
    names = [str(x) for x in names]
    n = len(ids)
    node_idx = np.asarray(node_idx, dtype=np.int64)
    tier = np.asarray(tier, dtype=np.int8)
    offset = np.asarray(offset, dtype=float)
    coords = np.asarray(coords, dtype=float)
    u = np.asarray(u, dtype=np.int64)
    v = np.asarray(v, dtype=np.int64)
    code = np.asarray(code, dtype=np.int8)
    flags = np.asarray(flags, dtype=np.bool_)
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source PSGC IDs in work/sources.npz")
    if not (len(names) == node_idx.size == tier.size == offset.size == n):
        raise ValueError("Town data arrays have inconsistent lengths")
    if coords.shape != (n, 2):
        raise ValueError("Town coordinates must have shape N x 2")
    if flags.ndim != 2 or flags.shape[1] < 2:
        raise ValueError("Graph flags array has an invalid shape")
    n_nodes = flags.shape[0]
    if code.shape != u.shape or v.shape != u.shape:
        raise ValueError("Graph edge types/endpoints have inconsistent lengths")
    if not np.isin(code, [ROAD, FERRY, GANGWAY]).all():
        raise ValueError("Graph contains unknown edge types")
    if not np.isin(tier, [-1, 0, 1]).all():
        raise ValueError("Town snap tier has unexpected codes")
    snap = tier >= 0
    if np.any((node_idx[snap] < 0) | (node_idx[snap] >= n_nodes)):
        raise ValueError("A snapped town has an invalid network node index")
    if np.any(~np.isfinite(offset[snap])) or np.any(offset[snap] < 0):
        raise ValueError("Snapped town has a nonfinite or negative connector length")

    full_ncomp, full_label = labels_for_graph(n_nodes, u, v)
    road_mask = code == ROAD
    road_ncomp, road_label = labels_for_graph(n_nodes, u[road_mask], v[road_mask])

    official = {str(x["psgc_code"]): x for x in roster}
    source = {key: i for i, key in enumerate(ids)}
    missing = sorted(set(official) - set(source))
    extra = sorted(set(source) - set(official))
    official_snapped = [key for key in official_codes if key in source and snap[source[key]]]
    full_counter = Counter(int(full_label[node_idx[source[key]]]) for key in official_snapped)
    road_counter = Counter(int(road_label[node_idx[source[key]]]) for key in official_snapped)
    full_reps = {}
    road_reps = {}
    for k in official_snapped:
        c = int(full_label[node_idx[source[k]]])
        rc = int(road_label[node_idx[source[k]]])
        full_reps[c] = min(full_reps.get(c, k), k)
        road_reps[rc] = min(road_reps.get(rc, k), k)
    ordered_full = sorted(full_counter, key=lambda c: (-full_counter[c], full_reps[c]))
    full_ids = {c: f"C{i:04d}" for i, c in enumerate(ordered_full, 1)}
    ordered_road = sorted(road_counter, key=lambda c: (-road_counter[c], road_reps[c]))
    road_ids = {c: f"R{i:04d}" for i, c in enumerate(ordered_road, 1)}

    connected_full = sum(choose2(v) for v in full_counter.values())
    connected_road = sum(choose2(v) for v in road_counter.values())
    if connected_full < connected_road:
        raise AssertionError("Full graph must be at least as connected as its road-only subgraph")

    total_expected = len(roster)
    official_snapped_count = len(official_snapped)
    all_pairs = choose2(total_expected)
    snapped_pairs = choose2(official_snapped_count)
    unresolved = all_pairs - snapped_pairs
    graph_disconnected = snapped_pairs - connected_full
    unrepresented_or_unsnapped = len(missing) + sum(1 for k in official_codes if k in source and not snap[source[k]])

    town_rows = []
    for ref in roster:
        key = str(ref["psgc_code"])
        if key not in source:
            town_rows.append(dict(psgc_code=key, name=str(ref.get("name", "")), province=str(ref.get("province", "")),
                                  state="MISSING_SOURCE", node_index="", snap_tier="", snap_offset_km="",
                                  lon="", lat="", component="", road_component="", reachable_official_towns=0,
                                  unreachable_or_unrepresented_official_towns=total_expected - 1,
                                  snap_warning="No locality point for this PSGC code"))
            continue
        j = source[key]
        if not snap[j]:
            town_rows.append(dict(psgc_code=key, name=str(ref.get("name", "")), province=str(ref.get("province", "")),
                                  state="UNSNAPPABLE", node_index="", snap_tier=-1, snap_offset_km="",
                                  lon=float(coords[j, 0]), lat=float(coords[j, 1]),
                                  component="", road_component="", reachable_official_towns=0,
                                  unreachable_or_unrepresented_official_towns=total_expected - 1,
                                  snap_warning="No network node inside snap radius"))
            continue
        node = int(node_idx[j])
        component = int(full_label[node])
        road_component = int(road_label[node])
        # The default repository option allows a fallback locality snap to a mid-route ferry point.
        midroute = bool(tier[j] == 1 and flags.shape[1] >= 3 and flags[node, 1] and not flags[node, 0] and not flags[node, 2])
        warning = "FERRY_MIDROUTE_SNAP_REQUIRES_REVIEW" if midroute else ("FERRY_FALLBACK_REQUIRES_REVIEW" if tier[j] == 1 else "")
        town_rows.append(dict(psgc_code=key, name=str(ref.get("name", "")), province=str(ref.get("province", "")),
                              state="SNAPPED", node_index=node, snap_tier=int(tier[j]),
                              snap_offset_km=round(float(offset[j]), 6),
                              lon=float(coords[j, 0]), lat=float(coords[j, 1]),
                              component=full_ids[component], road_component=road_ids[road_component],
                              reachable_official_towns=full_counter[component] - 1,
                              unreachable_or_unrepresented_official_towns=total_expected - full_counter[component],
                              snap_warning=warning))

    members = {c: [] for c in ordered_full}
    id_to_original = {display_id: original for original, display_id in full_ids.items()}
    for row in town_rows:
        if row["state"] == "SNAPPED":
            members[id_to_original[row["component"]]].append(row)
    components = []
    for c in ordered_full:
        example = sorted(members[c], key=lambda r: str(r["psgc_code"]))[:5]
        components.append(dict(component=full_ids[c], towns=full_counter[c],
                               percent_of_official=round(100 * full_counter[c] / total_expected, 3),
                               sample_towns="; ".join(f"{r['name']} ({r['psgc_code']})" for r in example)))

    reps = {c: sorted(members[c], key=lambda r: str(r["psgc_code"]))[0] for c in ordered_full}
    # Exact 100 heaviest component pairs in O(C log C), not O(C^2).
    # Essential when auditing up to 42,010 barangay origins.
    frontier = [(-full_counter[ordered_full[i]] * full_counter[ordered_full[i + 1]], i, i + 1)
                for i in range(len(ordered_full) - 1)]
    heapq.heapify(frontier)
    top_barrier_pairs = []
    while frontier and len(top_barrier_pairs) < 100:
        _, i, j = heapq.heappop(frontier)
        top_barrier_pairs.append((ordered_full[i], ordered_full[j]))
        if j + 1 < len(ordered_full):
            heapq.heappush(frontier, (-full_counter[ordered_full[i]] * full_counter[ordered_full[j+1]], i, j+1))
    barriers = [dict(component_a=full_ids[a], component_b=full_ids[b],
                     town_a=f"{reps[a]['name']} ({reps[a]['psgc_code']})",
                     town_b=f"{reps[b]['name']} ({reps[b]['psgc_code']})",
                     disconnected_official_pairs=full_counter[a] * full_counter[b])
                for a, b in top_barrier_pairs]

    road_ferry_pass = (not missing and not extra and official_snapped_count == total_expected and len(full_counter) == 1)
    fallback_count = sum(r["snap_tier"] == 1 for r in town_rows if r["state"] == "SNAPPED")
    midroute_count = sum(r["snap_warning"] == "FERRY_MIDROUTE_SNAP_REQUIRES_REVIEW" for r in town_rows)
    report = {
        "status": "PASS_MODELED_CONNECTIVITY" if road_ferry_pass else "FAIL_INCOMPLETE_OR_DISCONNECTED",
        "interpretation": "Connectivity of an undirected modeled graph only; NOT proof of an operating physical road/ferry journey",
        "official_localities": total_expected,
        "input_localities": n,
        "missing_official_localities": len(missing),
        "missing_psgc_codes": missing,
        "extra_source_localities": len(extra),
        "extra_psgc_codes": extra,
        "snapped_official_localities": official_snapped_count,
        "unsnappable_official_localities": total_expected - len(missing) - official_snapped_count,
        "official_components_road_ferry": len(full_counter),
        "official_components_road_only": len(road_counter),
        "largest_road_ferry_component_towns": max(full_counter.values(), default=0),
        "total_graph_nodes": n_nodes,
        "total_graph_edges": int(len(u)),
        "road_edges": int(np.count_nonzero(road_mask)),
        "ferry_edges": int(np.count_nonzero(code == FERRY)),
        "gangway_edges": int(np.count_nonzero(code == GANGWAY)),
        "all_graph_components": full_ncomp,
        "all_road_only_components": road_ncomp,
        "official_unordered_town_pairs": all_pairs,
        "reachable_pairs_road_ferry": connected_full,
        "reachable_pairs_road_only": connected_road,
        "additional_pairs_connected_by_ferries_or_gangways": connected_full - connected_road,
        "snapped_but_disconnected_pairs": graph_disconnected,
        "unresolved_pairs_missing_or_unsnappable": unresolved,
        "pairs_not_proven_reachable": graph_disconnected + unresolved,
        "ferry_fallback_snaps_requiring_review": fallback_count,
        "midroute_ferry_snaps_requiring_review": midroute_count,
        "max_snap_offset_km": max((r["snap_offset_km"] for r in town_rows if r["state"] == "SNAPPED"), default=None),
    }
    return report, town_rows, components, barriers


def _csv(path, rows, columns):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def self_test():
    def mock(roster_codes, src_codes, nodes, tiers, edges, codes):
        roster = [dict(psgc_code=k, name=f"Town {k}", province="Test") for k in roster_codes]
        idx = np.array(nodes, dtype=np.int64)
        t = np.array(tiers, dtype=np.int8)
        flags = np.zeros((4, 3), dtype=np.bool_)
        flags[[0, 1, 2], 0] = True
        flags[3, 1] = True
        flags[3, 2] = True
        u = np.array([x for x, _ in edges], dtype=np.int64)
        v = np.array([y for _, y in edges], dtype=np.int64)
        return audit(roster, src_codes, src_codes, idx, t, np.where(t >= 0, 0.1, np.inf),
                     np.zeros((len(src_codes), 2)), u, v, np.array(codes, dtype=np.int8), flags)[0]
    a, b, c = "0000000001", "0000000002", "0000000003"
    r = mock([a, b], [a, b], [0, 3], [0, 1], [(0, 1), (1, 2), (2, 3)], [0, 1, 2])
    assert r["status"] == "PASS_MODELED_CONNECTIVITY" and r["reachable_pairs_road_ferry"] == 1
    assert r["reachable_pairs_road_only"] == 0
    r = mock([a, b], [a, b], [0, 3], [0, 0], [(0, 1), (2, 3)], [0, 0])
    assert r["status"].startswith("FAIL") and r["snapped_but_disconnected_pairs"] == 1
    r = mock([a, b], [a, b], [0, -1], [0, -1], [(0, 1)], [0])
    assert r["unresolved_pairs_missing_or_unsnappable"] == 1 and r["unsnappable_official_localities"] == 1
    r = mock([a, b, c], [a, b], [0, 1], [0, 0], [(0, 1)], [0])
    assert r["missing_official_localities"] == 1 and r["unresolved_pairs_missing_or_unsnappable"] == 2
    r = mock([a, b], [a, b, c], [0, 1, 2], [0, 0, 0], [(0, 1), (1, 2)], [0, 0])
    assert r["extra_source_localities"] == 1 and r["status"].startswith("FAIL")
    print("SELF-TEST PASS: connected, disconnected, unsnappable, missing, extra-source cases")


def execute(args, *, work_override=None, output_override=None, verified_from_caller=False):
    """Audit stage-1 outputs; return 0=PASS, 1=FAIL, 2=NOT READY/diagnostic.

    The run.py wrapper has already checked current source hashes and settings.
    The standalone CLI performs the same check independently.
    """
    repo = args.repo.expanduser().resolve()
    work = Path(work_override).expanduser().resolve() if work_override is not None else args.workspace.expanduser().resolve() / "work"
    out = Path(output_override).expanduser().resolve() if output_override is not None else args.workspace.expanduser().resolve() / "output" / "connectivity_audit"
    roster_path = repo / "reference" / "psgc" / "psgc_q2_2026_localities.json"
    boundary_level = "town"
    selected_path = args.workspace.expanduser().resolve() / "data" / "current_boundaries.json"
    if selected_path.is_file():
        selected = json.loads(selected_path.read_text(encoding="utf-8"))
        boundary_level = selected.get("boundary_level", "town")
        if boundary_level != "town":
            from net_distance_ph.boundary_levels import LEVELS
            if boundary_level not in LEVELS:
                raise ValueError(f"Unknown selected boundary level: {boundary_level}")
            relative = Path(selected.get("roster", ""))
            if not selected.get("roster") or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe or missing selected level-roster path")
            roster_path = (selected_path.parent / relative).resolve()
            roster_path.relative_to(selected_path.parent.resolve())
            from net_distance_ph.boundaries import sha256
            if selected.get("roster_sha256") != sha256(roster_path):
                raise ValueError("Selected level roster checksum differs from boundary pointer")
    if boundary_level == "town" and not roster_path.is_file():
        # Installed wheel: pinned PSA reference is a separately packaged module.
        from reference import psgc as pinned_psgc
        roster_path = Path(pinned_psgc.__file__).resolve().parent / "psgc_q2_2026_localities.json"
    graph_path = work / "graph_full.npz"
    sources_path = work / "sources.npz"
    if not roster_path.is_file():
        print(f"NOT READY: Official PSGC roster missing: {roster_path}", file=sys.stderr)
        return 2
    if not graph_path.is_file() or not sources_path.is_file():
        print("NOT READY: No matching saved stage-1 national graph/source files.", file=sys.stderr)
        print(f"Looked for:\n  {graph_path}\n  {sources_path}", file=sys.stderr)
        print("First run in the repository directory: python run.py graph --verbose", file=sys.stderr)
        print("If boundary reconciliation fails, review the audit; do NOT proceed to matrix.", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    # Reuse this project's own integrity checks. This catches stale stage-1
    # snapshots, changed model parameters, and modified or absent input files.
    if not args.skip_provenance and not verified_from_caller:
        try:
            sys.path.insert(0, str(repo))
            import config as C
            import run as runner
            C.WORK = work
            C.DATA = args.workspace.expanduser().resolve() / "data"
            runner.DATA_EXPLICIT = False
            runner._require_stage1_current()
        except (Exception, SystemExit) as exc:
            print(f"NOT READY: Graph provenance check failed: {exc}", file=sys.stderr)
            return 2
    with roster_path.open("r", encoding="utf-8") as f:
        roster_file = json.load(f)
    if boundary_level == "town":
        if roster_file.get("schema") != "net-distance-ph/psgc-localities/v1":
            raise ValueError("Unexpected reference/psgc roster schema")
        expected_count = 1642
    else:
        from net_distance_ph.boundary_levels import COUNT
        if (roster_file.get("schema") != "net-distance-ph/psgc-selected-level/v1"
                or roster_file.get("boundary_level") != boundary_level):
            raise ValueError("Selected level-roster schema/level differs from boundary pointer")
        expected_count = COUNT[boundary_level]
    roster = roster_file.get("localities")
    if not isinstance(roster, list) or len(roster) != expected_count:
        raise ValueError(f"Selected roster must contain exactly {expected_count} {boundary_level} units")
    with np.load(sources_path, allow_pickle=False) as s:
        ids, names = s["ids"], s["names"]
        node_idx, tier = s["node_idx"], s["tier"]
        offset, coords = s["offset"], s["lonlat"]
        recorded_data_root = str(s["data_root"]) if "data_root" in s.files else ""
        manifest = json.loads(str(s["input_manifest"])) if "input_manifest" in s.files else {}
    with np.load(graph_path, allow_pickle=False) as g:
        u, v, code, flags = g["u"], g["v"], g["code"], g["flags"]

    report, towns, components, barriers = audit(roster, ids, names, node_idx, tier, offset, coords, u, v, code, flags)
    if args.skip_provenance:
        # A diagnostic run using unchecked files must never claim an audited PASS.
        report["status"] = "UNVERIFIED_PROVENANCE_DIAGNOSTIC_ONLY"
    report.update({
        "date_utc": datetime.now(timezone.utc).isoformat(),
        "repository": str(repo), "workspace": str(args.workspace.expanduser().resolve()),
        "boundary_level": boundary_level, "selected_roster": str(roster_path),
        "source_data_root": recorded_data_root,
        "source_fingerprint": manifest.get("fingerprint"),
        "provenance_checked": verified_from_caller or not args.skip_provenance,
        "analysis_seconds": round(time.monotonic() - t0, 3),
        "note": "Road-only result treats ferry-only snapped nodes as isolated. A road/ferry path is modeled undirected reachability, not live service or feasible transfer.",
    })
    try:
        report["code_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        report["code_commit"] = None

    out.mkdir(parents=True, exist_ok=True)
    _csv(out / "town_connectivity.csv", towns,
         ["psgc_code", "name", "province", "state", "node_index", "snap_tier", "snap_offset_km", "lon", "lat", "component", "road_component", "reachable_official_towns", "unreachable_or_unrepresented_official_towns", "snap_warning"])
    _csv(out / "component_summary.csv", components, ["component", "towns", "percent_of_official", "sample_towns"])
    _csv(out / "barrier_examples.csv", barriers, ["component_a", "component_b", "town_a", "town_b", "disconnected_official_pairs"])
    # Publish the JSON verdict last, once its CSV companions have been written.
    tmp_report = out / "connectivity_report.json.tmp"
    tmp_report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_report.replace(out / "connectivity_report.json")

    print(f"RESULT: {report['status']}")
    print(f"PSGC coverage: {report['input_localities']} input; {report['official_localities']} official; {report['missing_official_localities']} missing; {report['extra_source_localities']} extra")
    print(f"Snapped official towns: {report['snapped_official_localities']} / {report['official_localities']}")
    print(f"Components containing towns: road-only {report['official_components_road_only']}; roads+ferries {report['official_components_road_ferry']}")
    print(f"Reachable unordered official pairs: {report['reachable_pairs_road_ferry']:,} / {report['official_unordered_town_pairs']:,}")
    print(f"Snapped but disconnected pairs: {report['snapped_but_disconnected_pairs']:,}")
    print(f"Pairs involving missing/unsnappable towns: {report['unresolved_pairs_missing_or_unsnappable']:,}")
    print(f"Additional pairs connected by ferries/gangways: {report['additional_pairs_connected_by_ferries_or_gangways']:,}")
    print(f"Review: {report['ferry_fallback_snaps_requiring_review']} fallback ferry snaps; {report['midroute_ferry_snaps_requiring_review']} mid-route snaps")
    print(f"Reports written to: {out}")
    if args.skip_provenance:
        return 2
    return 0 if report["status"] == "PASS_MODELED_CONNECTIVITY" else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1], help="path to checked-out net-distance-ph repository")
    p.add_argument("--workspace", type=Path, default=Path.home() / "Documents" / "net-distance-ph", help="workspace with work/ and output/ folders")
    p.add_argument("--work", type=Path, help="override workspace/work (graph outputs)")
    p.add_argument("--out", type=Path, help="override full output directory for connectivity reports")
    p.add_argument("--self-test", action="store_true", help="run ONLY small synthetic unit tests; never access national input files")
    p.add_argument("--skip-provenance", action="store_true", help="do not check input hashes; DIAGNOSTIC ONLY, not publication-grade")
    a = p.parse_args()
    try:
        if a.self_test:
            self_test()
            return 0
        return execute(a, work_override=a.work, output_override=a.out)
    except (ValueError, OSError, KeyError, MemoryError, AssertionError, ImportError) as exc:
        print(f"AUDIT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
