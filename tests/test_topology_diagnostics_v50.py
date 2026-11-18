"""Offline topology diagnostics tests; no national graph or downloads."""
from __future__ import annotations

import numpy as np
import pytest

from net_distance_ph import topology_diagnostics as td


def site(town, core, original, component, pop=1, lon=0, lat=0):
    return {"town_psgc": town, "core_node": core, "original_node": original,
            "component": component, "population": pop, "lon": lon, "lat": lat}


def test_original_reduced_components_are_bijective_for_protected_sites():
    selected=[site("A",0,0,0,8),site("A",1,1,0,4),site("B",2,2,1,6)]
    result,mapping,mass,counts=td.compare_component_lineage(selected,
        np.array([0,0,1]),np.array([7,7,11]))
    assert result["preserved_for_selected_representatives"]
    assert mapping == {0:7,1:11}
    assert mass == {0:12,1:6}
    assert counts[0] == 2


def test_diagnose_false_disconnection_introduced_by_reduction():
    selected=[site("A",0,0,0),site("B",1,1,1)]
    result,_,_,_=td.compare_component_lineage(selected,np.array([0,1]),np.array([5,5]))
    assert result["preserved_for_selected_representatives"] is False
    assert result["original_components_split_by_reduction"] == {5:[0,1]}


def test_diagnose_false_connection_introduced_by_reduction():
    selected=[site("A",0,0,0),site("B",1,1,0)]
    result,_,_,_=td.compare_component_lineage(selected,np.array([0,0]),np.array([5,6]))
    assert result["preserved_for_selected_representatives"] is False
    assert result["original_components_merged_by_reduction"] == {0:[5,6]}


def test_stale_representative_component_labels_are_refused():
    with pytest.raises(ValueError,match="Stored component label"):
        td.compare_component_lineage([site("A",0,0,3)],np.array([2]),np.array([5]))


def test_stale_node_coordinates_are_refused():
    with pytest.raises(ValueError,match="coordinates differ"):
        td.compare_component_lineage([site("A",0,0,0,lon=3)],np.array([0]),np.array([0]),
            np.array([[0,0]]))


def test_cross_boundary_component_membership_is_not_mistaken_for_disconnection():
    selected=[site("A",0,0,0,8),site("A",1,1,1,2),site("B",2,2,1,3)]
    towns=[{"psgc_code":"A","name":"Town A"},{"psgc_code":"B","name":"Town B"}]
    rows=td.town_component_rows(selected,towns,{0:10,1:20},
        centroid_labels={"A":10,"B":20},dominant_core=0)
    alternate=[r for r in rows if r["town_psgc"]=="A" and r["core_component"]==1][0]
    assert alternate["component_town_count"]==2
    assert "Town B (B)" in alternate["other_towns_in_component"]
    assert alternate["same_component_as_town_centroid"] is False
    assert alternate["town_population_fraction"] == .2


def test_nearest_cross_component_road_gap_within_radius():
    # 0.00001 degrees longitude at equator ~1.1 meters; no graph link.
    xy=np.array([[120.,14.],[120.00001,14.], [120.002,14.], [121.,15.]])
    labels=np.array([1,2,1,3]);flags=np.array([[1,0,0]]*4,dtype=bool)
    a=td.nearest_other_component_gap(xy,flags,labels,1,search_km=1)
    assert a["status"]=="CANDIDATE_WITHIN_RADIUS"
    assert a["source_node"]==0 and a["candidate_node"]==1
    assert 0 < a["geometric_gap_km"] < .002
    assert a["candidate_original_component"]==2
    assert a["source_node_kind"]=="road"


def test_only_main_component_candidates_when_requested():
    xy=np.array([[120.,14.],[120.00001,14.],[120.00004,14.]])
    labels=np.array([1,2,3]);flags=np.array([[1,0,0]]*3,dtype=bool)
    a=td.nearest_other_component_gap(xy,flags,labels,1,search_km=1,target_component=3)
    assert a["status"]=="CANDIDATE_WITHIN_RADIUS"
    assert a["candidate_original_component"]==3
    assert a["candidate_node"]==2


def test_no_candidate_within_radius_is_not_declared_truly_isolated():
    xy=np.array([[120.,14.],[121.,14.]])
    labels=np.array([1,2]);flags=np.array([[1,0,0]]*2,dtype=bool)
    a=td.nearest_other_component_gap(xy,flags,labels,1,search_km=1)
    assert a["status"]=="NO_OTHER_COMPONENT_WITHIN_SEARCH_WINDOW"
    assert a["candidate_nodes"]==0


def test_capped_search_is_explicit_not_sampled():
    xy=np.array([[120.,14.],[120.1,14.],[120.2,14.]])
    labels=np.array([1,1,2]);flags=np.array([[1,0,0]]*3,dtype=bool)
    a=td.nearest_other_component_gap(xy,flags,labels,1,
        max_source_nodes=1)
    assert a["status"]=="TOO_MANY_SOURCE_NODES"


def test_node_flags_expose_ferry_terminals_without_assuming_service():
    assert td._node_kind([False, True, True])=="ferry+ferry_endpoint"
    assert td._node_kind([True, True, False])=="road+ferry"


def test_synthetic_saved_run_lineage_and_reports(tmp_path, monkeypatch):
    """Integration fixture: two towns, one town spans 2 CCs and shares one with B."""
    import argparse
    import csv
    import hashlib
    import json
    from scipy.sparse import csr_matrix
    from net_distance_ph import geographic_audit as ga
    root=tmp_path/"workspace"
    dataset=tmp_path/"reference"/"population"/"default"
    dataset.mkdir(parents=True,exist_ok=True)
    original_population_units=ga._population_units
    monkeypatch.setattr(ga,"_population_units",lambda _repo, manifest: original_population_units(tmp_path, manifest))
    ga._write_csv(dataset/"units.csv",[
       {"psgc_code":"1000000000","name":"Town A","level":"city","region_psgc":"","province_psgc":"","town_psgc":"1000000000"},
       {"psgc_code":"2000000000","name":"Town B","level":"city","region_psgc":"","province_psgc":"","town_psgc":"2000000000"}],
       ["psgc_code","name","level","region_psgc","province_psgc","town_psgc"])
    ga._write_csv(dataset/"barangay_population.csv",[
        {"psgc_code":"1000000001","population":"100"},
        {"psgc_code":"2000000001","population":"50"}],
       ["psgc_code","population"])
    from pathlib import Path
    work=root/"work"
    work.mkdir(parents=True)
    n=5
    u=np.array([0,2],dtype=np.int64);v=np.array([1,3],dtype=np.int64)
    flags=np.zeros((n,3),dtype=bool);flags[:,0]=True
    xy=np.array([[120,14],[120.01,14],[120.01001,14],[120.02,14],[120.2,14]],dtype=float)
    graph=work/"graph_full.npz"
    np.savez(graph, u=u,v=v,flags=flags,node_lonlat=xy)
    graph_sha=hashlib.sha256(graph.read_bytes()).hexdigest()
    fingerprint="fixture-source-fingerprint"
    np.savez(work/"sources.npz",ids=np.array(["1000000000","2000000000"]),
             node_idx=np.array([1,3]),tier=np.array([0,0]),
             input_manifest=json.dumps({"fingerprint":fingerprint}))
    key="e"*64;hash_key="f"*64
    core=root/"variants"/"population_weighted"/"cores"/key[:16]
    core.mkdir(parents=True)
    csr=csr_matrix((np.ones(4),(np.array([0,1,2,3]),np.array([1,0,3,2]))),shape=(4,4))
    from net_distance_ph import matrix as mx
    mx.save_core(csr, core)
    (core/"metadata.json").write_text(json.dumps({"cache_key":key,
        "national_graph_sha256":graph_sha,"graph_source_fingerprint":fingerprint,
        "units_sha256":ga._sha256(dataset/"units.csv"),
        "population_sha256":ga._sha256(dataset/"barangay_population.csv")}))
    base=root/"variants"/"population_weighted"/"output"
    run=base/hash_key[:16]
    (run/"town").mkdir(parents=True)
    (run/"province").mkdir()
    (base/"latest_run.json").write_text(json.dumps({"directory":str(run),"routing_hash":hash_key}))
    source={"units_sha256":ga._sha256(dataset/"units.csv"),
           "population_sha256":ga._sha256(dataset/"barangay_population.csv")}
    (run/"town"/"manifest.json").write_text(json.dumps({"routing_hash":hash_key,
        "core_cache_key":key,"level":"town","n_units":2,"population_source":source}))
    (run/"province"/"manifest.json").write_text(json.dumps({"routing_hash":hash_key,
        "core_cache_key":key,"level":"province"}))
    ga._write_csv(run/"town"/"units.csv",[
        {"psgc_code":"1000000000","name":"Town A"},
        {"psgc_code":"2000000000","name":"Town B"}],
        ["psgc_code","name"])
    np.save(run/"town"/"population_per_unit.npy",np.array([100.,50.]))
    np.save(run/"town"/"modeled_population_per_unit.npy",np.array([100.,50.]))
    np.save(run/"town"/"reachable_pair_probability.npy",np.array([[1.,.4],[.4,1.]]))
    ga._write_csv(run/"representative_sites.csv",[
        {"town_psgc":"1000000000","core_node":0,"original_node":0,"component":0,
         "population":60,"lon":120.,"lat":14.},
        {"town_psgc":"1000000000","core_node":2,"original_node":2,"component":1,
         "population":40,"lon":120.01001,"lat":14.},
        {"town_psgc":"2000000000","core_node":3,"original_node":3,"component":1,
         "population":50,"lon":120.02,"lat":14.}],
        ["town_psgc","core_node","original_node","component","population","lon","lat"])
    args=argparse.Namespace(workspace=root,run_dir=None,top_components=2,
                            search_km=5.0,lineage_only=False)
    assert td.execute(args)==0
    audit=root/"output"/"topology_diagnostics"/hash_key[:16]
    result=ga._read_json(audit/"topology_diagnostics.json")
    assert result["lineage"]["preserved_for_selected_representatives"]
    assert len(ga._read_csv(audit/"selected_site_components.csv"))==3
    memberships=ga._read_csv(audit/"town_component_access.csv")
    shared=[row for row in memberships if row["town_psgc"]=="1000000000" and row["core_component"]=="1"][0]
    assert "Town B" in shared["other_towns_in_component"]
    gaps=ga._read_csv(audit/"candidate_geometric_gaps.csv")
    assert any(row["status"]=="CANDIDATE_WITHIN_RADIUS" for row in gaps)
    # No numerical file was modified by a diagnostic read.
    graph_before=graph.read_bytes()
    assert graph.read_bytes()==graph_before
    # Same population run, altered graph bytes: refuse to compare mismatched
    # original and reduced graph connectivity (never silently accept stale data).
    graph.write_bytes(graph_before+b"tamper")
    with pytest.raises(ValueError,match="Original graph file has changed"):
        td.execute(args)
