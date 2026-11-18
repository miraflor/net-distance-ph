"""Offline tests for the read-only geographic/population audit (no national data)."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from net_distance_ph import geographic_audit as ga


def _town(code, name):
    return {"psgc_code":code,"name":name}


def test_unreachable_partial_and_unmodeled_are_distinct(tmp_path):
    towns=[_town("0000000001","Alpha"),_town("0000000002","Beta"),_town("0000000003","Gamma")]
    p=np.array([10.,20.,5.])
    modeled=np.array([10.,20.,0.])
    prob=np.array([[1.,0.5,0.],[0.5,1.,0.],[0.,0.,0.]])
    summary, zero,partial=ga.pair_reachability(towns,p,modeled,prob,top=4,
                                                all_pairs_path=tmp_path/"noncomplete.csv")
    assert summary["pair_classification"]["PARTIAL_MODELED_REACHABILITY"]==1
    assert summary["pair_classification"]["UNMODELED_PAIR"]==2
    assert not zero and len(partial)==1
    assert summary["population_pair_mass"]["modeled"] == 200
    assert summary["population_pair_mass"]["reachable_modeled"] == 100
    assert summary["population_pair_mass"]["unreachable_modeled"] == 100
    assert len(ga._read_csv(tmp_path/"noncomplete.csv"))==3


def test_distinguish_no_modeled_route_from_missing_observation():
    u=[_town("01","A"),_town("02","B"),_town("03","C")]
    pop=[100, 100, 100];model=[100, 100, 100]
    coverage=np.eye(3)
    summary, zero, partial=ga.pair_reachability(u,pop,model,coverage,top=1)
    assert summary["pair_classification"]["ZERO_MODELED_REACHABILITY"]==3
    assert len(zero)==1 and not partial
    assert zero[0]["status"]=="ZERO_MODELED_REACHABILITY"


def test_full_access_except_unsnappable_population_is_not_partial():
    units=[_town("1","A"),_town("2","B")]
    summary,_,partial=ga.pair_reachability(units,[10,10],[8,9],np.array([[1,0.72],[0.72,1]]))
    assert summary["pair_classification"]["COMPLETE_MODELED_REACHABILITY"]==1
    assert not partial
    assert summary["population_pair_mass"]["unmodeled"] == 28


def test_multiple_component_town_not_collapsed_to_single_cc():
    units=[_town("1","A"),_town("2","B")]
    sites=[{"town_psgc":"1","component":2,"population":4},
           {"town_psgc":"1","component":3,"population":6},
           {"town_psgc":"2","component":3,"population":20}]
    summary,comp,membership=ga.site_component_audit(sites,units,[10,20])
    assert summary["populated_components"]==2
    assert summary["towns_in_multiple_components"]==1
    assert sum(r["modeled_population"] for r in membership if r["town_psgc"]=="1")==10
    with pytest.raises(ValueError,match="population mismatch"):
        ga.site_component_audit(sites,units,[9,20])


def test_province_exclusions_use_explicit_hierarchy_not_guessing():
    units=[_town("0100000000","Alpha"),_town("0200000000","Beta")]
    refs={"0100000000":{"name":"Alpha","level":"city","province_psgc":"", "region_psgc":"1100000000"},
          "0200000000":{"name":"Beta","level":"municipality","province_psgc":"2100000000","region_psgc":"1100000000"},
          "1100000000":{"name":"Region","level":"region"}}
    excludes=ga.province_exclusions(units,refs,[200,100],[180,100],{})
    assert len(excludes)==1
    assert excludes[0]["modeled_population"]==180
    assert excludes[0]["unmodeled_population"]==20
    assert not ga.province_exclusions(units,refs,[200,100],[180,100],{"0100000000":"2100000000"})


def test_offshore_terminal_classification_does_not_claim_graph_disconnection():
    # Nodes: 0 road, 1 ferry offshore terminal, 2 ferry offshore terminal, 3 road.
    flags=np.array([[1,0,0],[0,1,1],[0,1,1],[1,0,0]],dtype=bool)
    u=np.array([1,1,2],dtype=int)
    v=np.array([0,2,3],dtype=int)
    # First is a gangway, second ferry, third a ferry: node 2 lacks direct gangway.
    codes=np.array([2,1,1],dtype=int)
    xy=np.array([[120,11],[120.1,11.1],[120.2,11.2],[120.3,11.3]],dtype=float)
    town=[{"psgc_code":"0000000011","name":"Town","lon":"120.11","lat":"11.11"}]
    summary,rows=ga.missing_ferry_gangways(flags,u,v,codes,xy,town)
    assert summary["offshore_ferry_terminal_nodes"]==2
    assert summary["directly_gangway_attached_nodes"]==1
    assert summary["without_direct_gangway_nodes"]==1
    assert rows[0]["original_graph_node"]==2
    assert rows[0]["nearest_official_town_psgc"]=="0000000011"


def test_routing_dir_boundary_and_manifest_validation(tmp_path):
    root=tmp_path/"workspace"/"variants"/"population_weighted"/"output"
    h="a"*64; run=root/h[:16]
    for level in ("town","province"):
        (run/level).mkdir(parents=True)
        (run/level/"manifest.json").write_text(json.dumps({"routing_hash":h,"core_cache_key":"one","level":level}))
    (root/"latest_run.json").write_text(json.dumps({"routing_hash":h,"directory":str(run)}))
    assert ga._resolve_run(tmp_path/"workspace")[0]==run.resolve()
    with pytest.raises(ValueError,match="immediate child"):
        ga._resolve_run(tmp_path/"workspace",tmp_path/"outside")
    (run/"province"/"manifest.json").write_text(json.dumps({"routing_hash":"b"*64,"core_cache_key":"one","level":"province"}))
    with pytest.raises(ValueError,match="Optional upper-tier manifest describes a different routing run"):
        ga._resolve_run(tmp_path/"workspace")


def test_hasher_rejects_modified_dataset(tmp_path):
    from net_distance_ph.geographic_audit import _population_units, _sha256
    ref=tmp_path/"reference"/"population"/"default";ref.mkdir(parents=True)
    units=ref/"units.csv";pop=ref/"barangay_population.csv"
    units.write_text("psgc_code,name,level,region_psgc,province_psgc,town_psgc\n")
    pop.write_text("psgc_code,population\n")
    record={"population_source":{"units_sha256":_sha256(units),"population_sha256":_sha256(pop)}}
    assert _population_units(tmp_path,record)[0]=={}
    units.write_text("tampered\n")
    with pytest.raises(ValueError,match="differs"):
        _population_units(tmp_path,record)


def test_main_rejects_bad_top_before_touching_workspace():
    with pytest.raises(SystemExit):
        ga.main(["--top","0"])


def test_full_population_audit_uses_existing_artifacts_without_touching_matrices(tmp_path, monkeypatch):
    repo=tmp_path/"source"/"net-distance-ph"
    (repo/"net_distance_ph").mkdir(parents=True)
    monkeypatch.setattr(ga,"__file__",str(repo/"net_distance_ph"/"geographic_audit.py"))
    d=repo/"reference"/"population"/"default";d.mkdir(parents=True)
    # Two towns, one province; second town has no province parent.
    rows=[{"psgc_code":"1100000000","name":"Region","level":"region","region_psgc":"1100000000","province_psgc":"","town_psgc":""},
          {"psgc_code":"1110000000","name":"Province","level":"province","region_psgc":"1100000000","province_psgc":"1110000000","town_psgc":""},
          {"psgc_code":"1110001000","name":"Town A","level":"municipality","region_psgc":"1100000000","province_psgc":"1110000000","town_psgc":"1110001000"},
          {"psgc_code":"1120001000","name":"Town B","level":"city","region_psgc":"1100000000","province_psgc":"","town_psgc":"1120001000"}]
    ga._write_csv(d/"units.csv",rows,("psgc_code","name","level","region_psgc","province_psgc","town_psgc"))
    ga._write_csv(d/"barangay_population.csv",[{"psgc_code":"1110001001","population":10}],("psgc_code","population"))
    hashes={"units_sha256":ga._sha256(d/"units.csv"),"population_sha256":ga._sha256(d/"barangay_population.csv")}
    workspace=tmp_path/"workspace"
    h="5"*64
    run=workspace/"variants"/"population_weighted"/"output"/h[:16]
    for level in ("town","province"):
        (run/level).mkdir(parents=True)
    ga._write_json(workspace/"variants"/"population_weighted"/"output"/"latest_run.json",
                  {"routing_hash":h,"directory":str(run)})
    ga._write_json(run/"town"/"manifest.json",{"routing_hash":h,"core_cache_key":"core","level":"town","n_units":2,"n_sites":2,"population_source":hashes})
    ga._write_json(run/"province"/"manifest.json",{"routing_hash":h,"core_cache_key":"core","level":"province","excluded_population_by_level":20,"province_crosswalk_sha256":None})
    ga._write_csv(run/"town"/"units.csv",[{"psgc_code":"1110001000","name":"Town A"},{"psgc_code":"1120001000","name":"Town B"}],("psgc_code","name"))
    np.save(run/"town"/"population_per_unit.npy",np.array([10.,20.]))
    np.save(run/"town"/"modeled_population_per_unit.npy",np.array([10.,15.]))
    prob=np.array([[1.,0.],[0.,0.75**2]])
    np.save(run/"town"/"reachable_pair_probability.npy",prob)
    ga._write_csv(run/"representative_sites.csv",[
        {"town_psgc":"1110001000","component":1,"population":10},
        {"town_psgc":"1120001000","component":2,"population":15}],
        ("town_psgc","component","population"))
    before=(run/"town"/"reachable_pair_probability.npy").read_bytes()
    args=__import__("argparse").Namespace(workspace=workspace,run_dir=None,province_crosswalk=None,top=10,all_pairs=False,skip_graph=True)
    assert ga.execute(args)==0
    report_path=workspace/"output"/"geographic_audit"/h[:16]/"geographic_audit.json"
    report=ga._read_json(report_path)
    assert report["province_accounting"]["excluded_town_count"]==1
    assert report["province_accounting"]["excluded_input_population"]==20
    assert report["province_accounting"]["excluded_modeled_population"]==15
    assert report["population_pair_reachability"]["pair_classification"]["ZERO_MODELED_REACHABILITY"]==1
    assert report["original_stage1_connectivity"] is None
    assert (run/"town"/"reachable_pair_probability.npy").read_bytes()==before
    assert len(ga._read_csv(report_path.parent/"province_excluded_towns.csv"))==1


def test_original_graph_ferry_audit_is_integrated_and_does_not_modify_sources(tmp_path, monkeypatch):
    # A minimal existing-run fixture is created by the previous integration test;
    # reproduce only the additional original-graph stage here using a delegated
    # execution test wrapper rather than a 12-million-node real graph.
    flags=np.array([[True,False,False],[False,True,True],
                    [False,True,True]],dtype=bool)
    xy=np.array([[120.0,12.0],[120.1,12.1],[120.2,12.2]])
    u=np.array([1,1],dtype=np.int64);v=np.array([0,2],dtype=np.int64)
    code=np.array([2,1],dtype=np.int8)
    report,rows=ga.missing_ferry_gangways(flags,u,v,code,xy,
        [{"psgc_code":"1110001000","name":"Town A","lon":120.0,"lat":12.0}])
    assert report["without_direct_gangway_nodes"]==1
    assert rows[0]["original_graph_node"]==2
    assert rows[0]["straight_line_to_nearest_town_km"] > 0
