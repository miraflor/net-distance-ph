"""Unit and CLI tests for a first-class, never-auto-building connectivity stage."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from net_distance_ph.connectivity import audit, choose2, execute, labels_for_graph


def fixture(*, official=3, ids=None, snapped_nodes=None, tiers=None, edges=(), edge_types=()):
    keys = [f"{i:010d}" for i in range(1, official + 1)]
    ids = keys if ids is None else ids
    snapped_nodes = list(range(len(ids))) if snapped_nodes is None else snapped_nodes
    tiers = [0] * len(ids) if tiers is None else tiers
    u = np.asarray([a for a, _ in edges], dtype=np.int64)
    v = np.asarray([b for _, b in edges], dtype=np.int64)
    n_nodes = max(4, max([a for edge in edges for a in edge] + [n for n in snapped_nodes if n >= 0] + [0]) + 1)
    flags = np.zeros((n_nodes, 3), dtype=np.bool_)
    flags[:, 0] = True
    roster = [dict(psgc_code=key, name=f"Town {key}", province="Test") for key in keys]
    return audit(roster, ids, ids, snapped_nodes, tiers,
                 np.where(np.asarray(tiers) >= 0, .1, np.inf),
                 np.zeros((len(ids), 2)), u, v, np.asarray(edge_types, dtype=np.int8), flags)


def test_two_towns_connected_by_roads():
    report, towns, _, barriers = fixture(official=2, edges=[(0, 1)], edge_types=[0])
    assert report["status"] == "PASS_MODELED_CONNECTIVITY"
    assert report["reachable_pairs_road_ferry"] == 1
    assert report["reachable_pairs_road_only"] == 1
    assert not barriers


def test_ferry_binds_road_components():
    report, towns, _, barriers = fixture(official=2, snapped_nodes=[0, 2],
                                          edges=[(0, 1), (1, 2)], edge_types=[0, 1])
    assert report["status"] == "PASS_MODELED_CONNECTIVITY"
    assert report["reachable_pairs_road_ferry"] == 1
    assert report["reachable_pairs_road_only"] == 0
    assert report["additional_pairs_connected_by_ferries_or_gangways"] == 1


def test_disconnected_town_pairs_and_barriers():
    report, towns, components, barriers = fixture(edges=[(0, 1)], edge_types=[0])
    assert report["status"].startswith("FAIL")
    assert report["snapped_but_disconnected_pairs"] == 2
    assert report["reachable_pairs_road_ferry"] == 1
    assert len(components) == 2
    assert barriers


def test_missing_and_unsnappable_are_not_pretended_reachable():
    report, towns, _, _ = fixture(ids=["0000000001", "0000000002"],
                                   snapped_nodes=[0, -1], tiers=[0, -1])
    assert report["missing_official_localities"] == 1
    assert report["unsnappable_official_localities"] == 1
    assert report["pairs_not_proven_reachable"] == 3
    assert {x["state"] for x in towns} == {"MISSING_SOURCE", "UNSNAPPABLE", "SNAPPED"}


def test_extra_source_ids_fail():
    report, *_ = fixture(official=2, ids=["0000000001", "0000000002", "0000000003"],
                         snapped_nodes=[0, 1, 2], edges=[(0,1), (1,2)], edge_types=[0, 0])
    assert report["extra_source_localities"] == 1
    assert report["status"].startswith("FAIL")


def test_malformed_edge_rejected():
    with pytest.raises(ValueError, match="outside the graph"):
        labels_for_graph(4, np.array([0]), np.array([8]))


def test_roster_wide_counts():
    report, *_ = fixture(official=1642, ids=[f"{i:010d}" for i in range(1, 1643)],
                         snapped_nodes=[0] * 1642, tiers=[0] * 1642)
    assert report["official_unordered_town_pairs"] == choose2(1642) == 1347261
    assert report["reachable_pairs_road_ferry"] == 1347261
    assert report["status"] == "PASS_MODELED_CONNECTIVITY"


def test_not_ready_no_output(tmp_path, capsys):
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    (repo / "reference" / "psgc").mkdir(parents=True)
    (repo / "reference" / "psgc" / "psgc_q2_2026_localities.json").write_text("{}")
    args = SimpleNamespace(repo=repo, workspace=workspace, skip_provenance=False)
    assert execute(args) == 2
    assert "NOT READY" in capsys.readouterr().err
    assert not (workspace / "output").exists()


def test_diagnostic_only_never_reports_verified_pass(tmp_path):
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    roster = [dict(psgc_code=f"{i:010d}", name=f"Town {i}", province="") for i in range(1, 1643)]
    rf = repo / "reference" / "psgc" / "psgc_q2_2026_localities.json"
    rf.parent.mkdir(parents=True)
    rf.write_text(json.dumps({"schema": "net-distance-ph/psgc-localities/v1", "localities": roster}))
    work = workspace / "work"
    work.mkdir(parents=True)
    ids = [x["psgc_code"] for x in roster]
    np.savez(work / "sources.npz", ids=ids, names=[x["name"] for x in roster],
             node_idx=np.zeros(1642, dtype=np.int64), tier=np.zeros(1642, dtype=np.int8),
             offset=np.zeros(1642), lonlat=np.zeros((1642, 2)), data_root="synthetic",
             input_manifest=json.dumps({"fingerprint": "synthetic-only"}))
    np.savez(work / "graph_full.npz", u=np.array([], dtype=np.int64), v=np.array([], dtype=np.int64),
             code=np.array([], dtype=np.int8), flags=np.array([[True, False, False]], dtype=bool))
    args = SimpleNamespace(repo=repo, workspace=workspace, skip_provenance=True)
    assert execute(args) == 2
    audit_dir = workspace / "output" / "connectivity_audit"
    result = json.loads((audit_dir / "connectivity_report.json").read_text())
    assert result["provenance_checked"] is False
    assert result["status"] == "UNVERIFIED_PROVENANCE_DIAGNOSTIC_ONLY"
    assert result["reachable_pairs_road_ferry"] == 1347261
    assert (audit_dir / "town_connectivity.csv").exists()
    assert (audit_dir / "component_summary.csv").exists()
    assert (audit_dir / "barrier_examples.csv").exists()
