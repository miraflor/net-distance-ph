"""v52 origin-snap audit tests: no downloads or changes to real workspace."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import hashlib
import importlib.util
import sys
import types

import numpy as np
import pytest


@pytest.fixture(scope='module')
def snap_module():
    # Import the real installed module in the user's checkout.
    from net_distance_ph import origin_snap_audit as s
    return s


def test_rank_mass_and_shared_town(snap_module):
    sites = [
       {'original_node':0,'population':100,'town_psgc':'A'},
       {'original_node':2,'population':25,'town_psgc':'A'},
       {'original_node':3,'population':20,'town_psgc':'B'},
       {'original_node':4,'population':12,'town_psgc':'C'},
       {'original_node':5,'population':7,'town_psgc':'D'},
    ]
    main,ordered,mass,towns = snap_module.rank_population_components(sites,np.array([4,4,8,8,12,12]))
    assert main == 4
    assert ordered == [8,12]
    assert mass[8] == 45 and towns[8] == {'A','B'}
    assert snap_module.select_priority_components(ordered,top=2) == [(1,8),(2,12)]
    assert snap_module.select_priority_components(ordered,ranks=[2,1]) == [(2,12),(1,8)]


@pytest.mark.parametrize('ranks', [[0],[3],[1,1],[]])
def test_invalid_ranks_fail(snap_module,ranks):
    with pytest.raises(ValueError,match='--ranks'):
        snap_module.select_priority_components([3,4],ranks=ranks)


def test_nearest_main_road_not_nearest_unrelated_road(snap_module):
    # 120.0005 is an irrelevant component, not one of the main-component roads.
    all_xy = np.array([[120.,14.],[120.0005,14.],[120.01,14.],[120.02,14.]])
    origin = np.array([[120.0004,14.],[120.019,14.]])
    nodes,d,arc = snap_module.nearest_main_roads(origin,all_xy[[0,2,3]],np.array([0,2,3]))
    assert nodes.tolist() == [0,3]
    assert d[0] > 0 and d[0] < .1
    assert d[1] > d[0] and np.all(arc > 0)


def test_empty_main_nodes_rejected(snap_module):
    with pytest.raises(ValueError,match='No valid main-component road'):
        snap_module.nearest_main_roads(np.array([[120.,14.]]),np.empty((0,2)),np.array([],dtype=int))


def test_same_radius_rule_preserved(snap_module):
    assert snap_module.assess_origin(100,0.2,0.4,0.4)['alternative_within_original_snap_radius']
    assert not snap_module.assess_origin(100,0.2,0.401,0.4)['alternative_within_original_snap_radius']
    assert snap_module.assess_origin(100,0.2,0.1,1)['alternative_closer_than_saved']
    assert not snap_module.assess_origin(100,0.2,0.1999,1)['alternative_closer_than_saved']
    assert snap_module.assess_origin(100,0.1,0.50000001,0.5,screen_distance=0.49999999)['alternative_within_original_snap_radius']


def test_geometry_hash_match_and_detect_mutation(snap_module,tmp_path):
    p=tmp_path/'points.csv';p.write_text('psgc_code,lon,lat\n100,120,14\n')
    digest=lambda x: hashlib.sha256(Path(x).read_bytes()).hexdigest()
    source={'.csv':digest(p)}
    assert snap_module.geometry_matches(p,source,digest)
    assert snap_module.resolve_geometry(tmp_path,tmp_path,{'points_file_sha256':source},p) == p
    p.write_text('psgc_code,lon,lat\n100,121,14\n')
    assert not snap_module.geometry_matches(p,source,digest)
    with pytest.raises(FileNotFoundError,match='No cached geometry'):
        snap_module.resolve_geometry(tmp_path,tmp_path,{'points_file_sha256':source},p)


def test_shapefile_sidecars_require_original_bytes(snap_module,tmp_path):
    p=tmp_path/'x.shp';p.write_bytes(b'one')
    dbf=tmp_path/'x.dbf';dbf.write_bytes(b'two')
    digest=lambda x: hashlib.sha256(Path(x).read_bytes()).hexdigest()
    recorded={'.shp':digest(p),'.dbf':digest(dbf)}
    assert snap_module.geometry_matches(p,recorded,digest)
    (tmp_path/'x.shx').write_bytes(b'extra')
    assert not snap_module.geometry_matches(p,recorded,digest)


def test_negative_or_missing_population_rejected(snap_module):
    with pytest.raises(ValueError):
        snap_module.rank_population_components([{'original_node':0,'population':0,'town_psgc':'A'}],np.array([0]))
    with pytest.raises(ValueError):
        snap_module.assess_origin(5,-1,.2,2)


def test_synthetic_saved_origin_alignment_and_mass(tmp_path, monkeypatch, snap_module):
    """End-to-end saved-file fixture; never modifies real geographic inputs."""
    import json
    from net_distance_ph import geographic_audit as ga
    from net_distance_ph import population_data as pd
    from net_distance_ph import topology_diagnostics as td
    from net_distance_ph import population_weighted as pw
    from net_distance_ph.geo import geodesic_km
    root=tmp_path/'workspace'
    result=root/'variants'/'population_weighted'/'output'/('f'*16)
    (result/'town').mkdir(parents=True)
    core=root/'variants'/'population_weighted'/'cores'/('e'*16)
    core.mkdir(parents=True)
    graphdir=root/'work';graphdir.mkdir()
    graph=graphdir/'graph_full.npz'
    locations=np.array([[120.,14.],[120.0002,14.],[120.006,14.],
                        [120.0062,14.],[120.1,14.]],dtype=float)
    flags=np.zeros((5,3),dtype=bool);flags[:,0]=True
    np.savez(graph,u=np.array([0,2],dtype=np.int32),v=np.array([1,3],dtype=np.int32),
             flags=flags,node_lonlat=locations)
    np.savez(graphdir/'sources.npz',input_manifest=json.dumps({'fingerprint':'safe-fixture'}))
    def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    points=tmp_path/'points.csv'
    names=['1010000001','1010000002','2020000001','3030000001']
    origins=locations[[0,2,3,4]]+np.array([[0.,0.00001]]*4)
    points.write_text('psgc_code,lon,lat\n'+''.join(
        f'{code},{xy[0]:.9f},{xy[1]:.9f}\n' for code,xy in zip(names,origins)),encoding='utf-8')
    populations=[100,40,30,10];nodes=[0,2,3,4]
    for name,value in [('original_index_per_bar.npy',np.array(nodes,dtype=np.int32)),
                       ('offset_per_bar.npy',np.asarray([float(geodesic_km(*origins[i],*locations[n])) for i,n in enumerate(nodes)])),
                       ('valid_barangay_mask.npy',np.ones(4,dtype=bool))]:
        np.save(core/name,value)
    pop_hashes={'units_sha256':'units-hash','population_sha256':'population-hash'}
    (core/'metadata.json').write_text(json.dumps({**{k:v for k,v in pop_hashes.items()},
       'cache_key':'e'*64,'national_graph_sha256':digest(graph),
       'graph_source_fingerprint':'safe-fixture','points_file_sha256':{'.csv':digest(points)},
       'max_snap_km':.3, 'n_unmodeled_barangays':0}),encoding='utf-8')
    units={f'{i}0':{'name':name,'level':'city'} for i,name in enumerate(('Town A','Town B','Town C'),1)}
    # Actual town keys arbitrary in the synthetic test; they are not official PSGC codes.
    town_codes=['1000000000','2000000000','3000000000']
    units={x:{'name':y,'level':'city'} for x,y in zip(town_codes,['A','B','C'])}
    rows=[{'psgc_code':name,'town_psgc':town,'population':pop,'barangay_name':name}
          for name,town,pop in zip(names,[town_codes[0],town_codes[0],town_codes[1],town_codes[2]],populations)]
    sites=[{'original_node':node,'population':pop,'town_psgc':row['town_psgc']}
           for node,pop,row in zip(nodes,populations,rows)]
    monkeypatch.setattr(ga,'_resolve_run',lambda workspace,requested:(result,{'core_cache_key':'e'*64},None,'f'*64), raising=False)
    monkeypatch.setattr(ga,'_population_units',lambda repo,manifest:(units,pop_hashes), raising=False)
    monkeypatch.setattr(ga,'_load_town_tables',lambda run:([{'psgc_code':x} for x in town_codes],None,np.array([140.,30.,10.]),None), raising=False)
    monkeypatch.setattr(snap_module,'resolve_geometry',lambda *a:points)
    monkeypatch.setattr(td,'_site_rows',lambda *a:sites)
    monkeypatch.setattr(pd,'load_plain_dataset',lambda *a:(units,rows,{'source':pop_hashes}))
    from argparse import Namespace
    assert snap_module.execute(Namespace(workspace=root,run_dir=None,points_file=points,top=2,ranks=[1,2])) == 0
    target=root/'output'/'origin_snap_sensitivity'/('f'*16)
    summary=json.loads((target/'origin_snap_sensitivity.json').read_text())
    assert summary['all_modeled_original_component_mass_verified']
    assert summary['selected_modeled_population'] == 80
    assert summary['barangay_origins_examined'] == 3
    assert (target/'affected_barangay_origins.csv').is_file()
