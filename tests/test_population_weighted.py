"""Offline numerical regression for the optional population-weighted model."""
from __future__ import annotations

import csv
import json
from types import SimpleNamespace
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from net_distance_ph import population_weighted as pop
from net_distance_ph import population_routing as route


def _toy_dataset(tmp_path, *, include_second=True, nonprovincial=False):
    from net_distance_ph.population_data import UNIT_COLUMNS, POP_COLUMNS, _write_csv
    region="0100000000"
    prov="0128000000"
    town="0128010000"
    pv="" if nonprovincial else prov
    units=[{"psgc_code":region,"name":"Region","level":"region","region_psgc":region,"province_psgc":"","town_psgc":""},
           {"psgc_code":prov,"name":"Province","level":"province","region_psgc":region,"province_psgc":prov,"town_psgc":""},
           {"psgc_code":town,"name":"Town","level":"municipality","region_psgc":region,"province_psgc":pv,"town_psgc":town},
           {"psgc_code":"0128010001","name":"One","level":"barangay","region_psgc":region,"province_psgc":pv,"town_psgc":town},
           {"psgc_code":"0128010002","name":"Two","level":"barangay","region_psgc":region,"province_psgc":pv,"town_psgc":town}]
    population=[{"psgc_code":"0128010001","population":17}]
    if include_second:population.append({"psgc_code":"0128010002","population":23})
    _write_csv(tmp_path/"units.csv",UNIT_COLUMNS,units)
    _write_csv(tmp_path/"barangay_population.csv",POP_COLUMNS,population)
    return town


def test_plain_population_and_parent_codes(tmp_path):
    from net_distance_ph.population_data import load_plain_dataset
    town=_toy_dataset(tmp_path)
    roster,rows,meta=load_plain_dataset(tmp_path)
    assert len(rows)==2
    assert sum(r["population"] for r in rows)==40
    assert set(r["town_psgc"] for r in rows)=={town}
    assert set(r["province_psgc"] for r in rows)=={"0128000000"}
    assert set(r["region_psgc"] for r in rows)=={"0100000000"}
    assert len(meta["source"]["population_sha256"])==64


def test_missing_population_does_not_become_zero(tmp_path):
    from net_distance_ph.population_data import load_plain_dataset
    _toy_dataset(tmp_path,include_second=False)
    with pytest.raises(ValueError,match="Population missing"):
        load_plain_dataset(tmp_path)
    roster,rows,meta=load_plain_dataset(tmp_path,allow_missing_population=True)
    assert len(rows)==1 and rows[0]["population"]==17
    assert meta["missing_population_psgc"]==["0128010002"]


def test_k1_is_exact_weighted_network_median():
    # Three candidate sites on line at 0,3,10 km. Heavy population at 3.
    x=np.array([0.,3.,10.]);D=np.abs(x[:,None]-x[None,:])
    sel,weights,access,err=pop.select_sites(D,[1,100,1],[0,0,0])
    assert sel.tolist()==[1] and weights.tolist()==[102]
    assert np.isclose(err,10/102)


def test_access_budget_produces_multiple_sites_and_protects_islands():
    D=np.array([[0,100,np.inf,np.inf],[100,0,np.inf,np.inf],
                [np.inf,np.inf,0,100],[np.inf,np.inf,100,0]],dtype=float)
    p=[1,1,1,1]
    selected,weights,access,error=pop.select_sites(D,p,[0]*4,max_sites=4,max_mean_access_km=0)
    assert len(selected)==4 and error==0
    selected,_,_,_=pop.select_sites(D,p,[0]*4,max_sites=4)
    assert len(selected)==2  # one per disconnected component, not one per town


def test_cap_fails_instead_of_silently_violating_bound():
    D=np.array([[0.,100.],[100.,0.]])
    with pytest.raises(ValueError,match="budget.*unmet"):
        pop.select_sites(D,[1,1],[0,0],max_sites=1,max_mean_access_km=1.)


def test_group_aggregation_from_population_pair_mass():
    # Different populations and disconnected reachable-pair fractions.
    numerator=np.array([[0.,20.,100.],[20.,0.,60.],[100.,60.,0.]])
    denom=np.array([[4.,2.,5.],[2.,9.,2.],[5.,2.,16.]])
    na,da=pop.combine_group_matrices(numerator,denom,[0,0,1],2)
    assert np.allclose(na,[[40,160],[160,0]])
    assert np.allclose(da,[[17,7],[7,16]])
    d,c=pop.finish_means(na,da,[5,5])
    assert np.isclose(d[0,1],160/7)
    assert np.isclose(c[0,1],7/25)


def test_conditional_mean_retains_true_nonzero_intraregional_diagonal():
    mat,reach=pop.finish_means(np.array([[4.,10.],[10.,0.]]),
                               np.array([[2.,2.],[2.,4.]]),[2,2])
    assert mat[0,0]==2 and mat[0,1]==5
    assert reach[0,1]==.5
    assert np.array_equal(mat,mat.T)


def test_early_terminated_network_dijkstra_exact_and_disconnected():
    # Connected 0-1-2-3 and detached 4-5; exact target stopping.
    a=np.array([0,1,2,4]);b=np.array([1,2,3,5]);w=np.array([2.,3.,4.,7.])
    rows=np.r_[a,b];cols=np.r_[b,a];dat=np.r_[w,w]
    from scipy.sparse import coo_matrix
    G=coo_matrix((dat,(rows,cols)),shape=(6,6)).tocsr()
    solver=route.TargetDijkstra(G)
    for src in (0,1,3,4,5):
        targets=np.array([0,1,2,3,4,5],dtype=np.int32)
        got=solver.get(src,targets)
        expected=dijkstra(G,directed=False,indices=src)[targets]
        assert np.array_equal(np.isfinite(got),np.isfinite(expected))
        assert np.array_equal(got[np.isfinite(got)],expected[np.isfinite(expected)])


def test_early_stopping_multiple_runs_do_not_mix_epochs():
    from scipy.sparse import csgraph
    G=csr_matrix(np.array([[0.,1.,0.],[1.,0.,2.],[0.,2.,0.]]))
    solver=route.TargetDijkstra(G)
    assert solver.get(0,np.array([1,2],dtype=np.int32)).tolist()==[1,3]
    assert solver.get(2,np.array([0,1],dtype=np.int32)).tolist()==[3,2]


def test_province_geographic_coding_not_fabricated(tmp_path):
    from net_distance_ph.population_data import load_plain_dataset
    _toy_dataset(tmp_path,nonprovincial=True)
    roster,rows,_=load_plain_dataset(tmp_path)
    assert set(r["province_psgc"] for r in rows)=={""}


def test_code10_preserves_zeroes_and_rejects_floats():
    assert pop.code10("128010001")=="0128010001"
    with pytest.raises(ValueError):pop.code10("128010001.0")


def test_invalid_point_csv_cannot_silently_drop_codes(tmp_path):
    f=tmp_path/"points.csv"
    f.write_text("psgc_code,lon,lat\n0128010001,121,14\n",encoding="utf-8")
    points=pop.read_points(f)
    with pytest.raises(ValueError,match="lack points"):
        pop.validate_join([{"psgc_code":"0128010002"}],points)


def test_disconnected_component_shortest_distances_not_assumed_finite():
    G=csr_matrix(np.array([[0.,5.,0.],[5.,0.,0.],[0.,0.,0.]]))
    solver=route.TargetDijkstra(G)
    d=solver.get(0,np.array([1,2],dtype=np.int32))
    assert d[0]==5 and np.isinf(d[1])


def test_town_medians_then_population_od_streaming(tmp_path, monkeypatch):
    # Small exact network. Production uses 1,642 towns, but same algorithms.
    from scipy.sparse import coo_matrix
    a=np.array([0,1,2]);b=np.array([1,2,3]);w=np.array([1.,2.,3.])
    G=coo_matrix((np.r_[w,w],(np.r_[a,b],np.r_[b,a])),shape=(4,4)).tocsr()
    monkeypatch.setattr(route.mx,"load_core",lambda directory, mmap=True:G, raising=False)
    core=tmp_path/"core";core.mkdir()
    np.save(core/"core_index_per_bar.npy",np.array([0,1,2,3],dtype=np.int32))
    np.save(core/"original_index_per_bar.npy",np.array([0,1,2,3],dtype=np.int32))
    np.save(core/"offset_per_bar.npy",np.zeros(4))
    np.save(core/"snapped_node_lonlat.npy",np.array([[120,15],[120.1,15],[120.2,15],[120.3,15]]))
    t1="0128010000";t2="0128020000"
    rows=[{"town_psgc":t1,"population":2},{"town_psgc":t1,"population":6},
          {"town_psgc":t2,"population":4},{"town_psgc":t2,"population":8}]
    roster={t1:{"geographic_level":"Mun","name":"Town One"},
            t2:{"geographic_level":"Mun","name":"Town Two"}}
    sites=route.build_town_sites(tmp_path,core,"toyhash",rows,roster,
              {"selection":{"max_mean_access_km":None,"max_sites_per_town":4}})
    assert len(sites)==2
    assert [(x["core_node"],x["population"]) for x in sites]==[(1,8),(3,12)]
    towns,N,R,_=route.compute_routing(tmp_path,core,sites,"toyhash",workers=1)
    assert towns==[t1,t2]
    assert np.isclose(N[0,1],8*12*5) and np.isclose(N[1,0],8*12*5)
    assert np.isclose(R[0,1],8*12)
    mat,reach=pop.finish_means(N,R,[8,12])
    assert mat[0,1]==mat[1,0]==5 and reach[0,1]==1
    # The routing checkpoint can resume without recomputing settled sources.
    towns2,N2,R2,_=route.compute_routing(tmp_path,core,sites,"toyhash",workers=1)
    assert np.array_equal(N,N2) and np.array_equal(R,R2)


def test_early_terminated_dijkstra_random_graph_property():
    from scipy.sparse import coo_matrix
    rng=np.random.default_rng(20261117)
    n=110
    a=rng.integers(0,n,350)
    b=rng.integers(0,n,350)
    good=a!=b
    a,b=a[good],b[good]
    w=rng.uniform(.01,30.,size=len(a))
    G=coo_matrix((np.r_[w,w],(np.r_[a,b],np.r_[b,a])),shape=(n,n)).tocsr()
    G.sum_duplicates()
    D=dijkstra(G,directed=False)
    engine=route.TargetDijkstra(G)
    for source in rng.choice(n,15,replace=False):
        chosen=rng.choice(n,18,replace=False).astype(np.int32)
        got=engine.get(source,chosen)
        ref=D[source,chosen]
        assert np.array_equal(np.isfinite(got),np.isfinite(ref))
        assert np.allclose(got[np.isfinite(got)],ref[np.isfinite(ref)],rtol=1e-13,atol=1e-12)


def test_output_manifest_and_error_bound_are_separate_from_centroid_run(tmp_path):
    sites=[{"town_psgc":"0128010000","population":8,"mean_access_km":.25},
           {"town_psgc":"0128020000","population":12,"mean_access_km":1.}]
    town_num=np.array([[0.,480.],[480.,0.]])
    town_den=np.array([[64.,96.],[96.,144.]])
    out=route._save_level(tmp_path,"town",[
           {"code":"0128010000","name":"A"},
           {"code":"0128020000","name":"B"}],np.array([0,1]),
           town_num,town_den,np.array([8.,12.]),sites,
           {"population_total":20,"source":{"agency":"PSA"}},
           route_hash="0123456789abcdef0123",core_key="coretoy")
    assert out==tmp_path/"output"/"0123456789abcdef"/"town"
    d=np.load(out/"distance_matrix_km.npy")
    reach=np.load(out/"reachable_pair_probability.npy")
    bound=np.load(out/"distance_approximation_bound_km.npy")
    assert d[0,1]==5. and reach[0,1]==1.
    assert np.isclose(bound[0,1],1.25)
    assert not (tmp_path/"output"/"distance_matrix_km.npy").exists()


def test_province_crosswalk_must_not_change_published_psgc_hierarchy(tmp_path):
    prov1="0128000000";prov2="0133000000";town="0128010000"
    roster={town:{"geographic_level":"Mun", "province_psgc":prov1},
            prov1:{"geographic_level":"Prov"},prov2:{"geographic_level":"Prov"}}
    f=tmp_path/"override.csv"
    f.write_text("town_psgc,province_psgc\n0128010000,0133000000\n",encoding="utf-8")
    with pytest.raises(ValueError,match="override existing PSGC province"):
        route._validated_province_crosswalk(f,roster)


def test_province_crosswalk_can_explicitly_attach_unassigned_independent_city(tmp_path):
    town="1380100000";prov="0354000000"
    roster={town:{"geographic_level":"City", "province_psgc":""},prov:{"geographic_level":"Prov"}}
    f=tmp_path/"geographic.csv"
    f.write_text(f"town_psgc,province_psgc\n{town},{prov}\n",encoding="utf-8")
    data,hashvalue=route._validated_province_crosswalk(f,roster)
    assert data=={town:prov} and len(hashvalue)==64


def test_unsnappable_population_remains_in_coverage_denominator(tmp_path):
    # One town has 10 residents represented by roads and 10 with no valid
    # snap. The matrix's conditional mean is calculable for represented
    # residents, but the population-pair coverage must be only 25%.
    t='0128010000'
    sites=[{'town_psgc':t,'core_node':0,'population':10,'mean_access_km':0.0}]
    extra={'population_total':20,'source':{'input_format':'plain CSV'}}
    route._save_level(tmp_path,'town',[{'code':t,'name':'Town'}],np.array([0]),
        np.array([[0.0]]),np.array([[100.0]]),np.array([10.0]),sites,extra,
        route_hash='test0000',core_key='testcore',full_town_pop=np.array([20.0]),town_codes=[t])
    root=tmp_path/'output'/'test0000'/'town'
    assert np.load(root/'population_per_unit.npy').tolist()==[20]
    assert np.load(root/'modeled_population_per_unit.npy').tolist()==[10]
    assert np.load(root/'origin_population_coverage.npy').tolist()==[0.5]
    assert np.load(root/'reachable_pair_probability.npy')[0,0]==.25
    assert json.loads((root/'manifest.json').read_text())['unmodeled_origin_population']==10


def test_fully_unsnappable_town_retains_empty_row(tmp_path):
    t1='0128010000';t2='0128020000'
    sites=[{'town_psgc':t1,'core_node':0,'population':10,'mean_access_km':0.0}]
    pop=np.array([10.,8.]);modeled=np.array([10.,0.]);
    route._save_level(tmp_path,'town',[{'code':t1,'name':'T1'},{'code':t2,'name':'T2'}],
        np.array([0,1]),np.zeros((2,2)),np.array([[100.,0.],[0.,0.]]),modeled,sites,
        {'population_total':18,'source':{}},route_hash='test0001',core_key='c',
        full_town_pop=pop,town_codes=[t1,t2])
    result=np.load(tmp_path/'output'/'test0001'/'town'/'distance_matrix_km.npy')
    reach=np.load(tmp_path/'output'/'test0001'/'town'/'reachable_pair_probability.npy')
    assert result.shape==(2,2) and np.isinf(result[1,0])
    assert np.all(reach[1,:]==0)


def test_points_core_excludes_invalid_snaps_with_audit(tmp_path,monkeypatch):
    """Failed snaps do not enter protected vertices or get fake road links."""
    import sys
    from types import ModuleType
    workspace=tmp_path/'workspace'
    work=workspace/'work';work.mkdir(parents=True)
    np.savez(work/'graph_full.npz',node_lonlat=np.array([[120.,14.],[120.1,14.],[120.2,14.]]),
             flags=np.ones((3,3),dtype=bool),u=np.array([0,1]),v=np.array([1,2]),w=np.array([1.,1.]))
    np.savez(work/'sources.npz',input_manifest=np.array(json.dumps({'fingerprint':'toy'})))
    pts=tmp_path/'point.csv';pts.write_text('psgc_code,lon,lat\n0128010001,120,14\n0128010002,120.1,14\n')
    rows=[{'psgc_code':'0128010001','town_psgc':'0128010000','population':15,'barangay_name':'A'},
          {'psgc_code':'0128010002','town_psgc':'0128010000','population':5,'barangay_name':'B'}]
    monkeypatch.setattr(route.gb,'snap_sources',lambda *a,**k:
        (np.array([0,-1]),np.array([0.,np.inf]),np.array([0,-1])),raising=False)
    monkeypatch.setattr(route.rd,'dedupe_edges',lambda u,v,w,n:(u,v,w),raising=False)
    fake_fast=ModuleType('net_distance_ph.fast_reduce')
    def reduce_fast(u,v,w,n,protected,progress=None):
        assert protected.tolist()==[0]
        return u,v,w,np.ones(n,dtype=bool)
    fake_fast.reduce_fast=reduce_fast
    monkeypatch.setitem(sys.modules,'net_distance_ph.fast_reduce',fake_fast)
    monkeypatch.setattr(route.rd,'compact',lambda u,v,w,alive,src:
        (u,v,w,3,np.array([0],dtype=np.int32)),raising=False)
    monkeypatch.setattr(route.mx,'build_csr',lambda *a:'test csr',raising=False)
    def save(csr,dest):
        for name in ('core_data','core_indices','core_indptr','core_shape'):
            np.save(dest/f'{name}.npy',np.array([1]))
    monkeypatch.setattr(route.mx,'save_core',save,raising=False)
    core,key=route.points_core(workspace,workspace/'variants'/'population_weighted',rows,
              {'data_file_sha256':'populationhash','source':{'units_sha256':'unitshash'}},pts,
              {'positions':np.array([[120.,14.],[120.1,14.]]),
               'max_snap_km':25.,'unsnappable_policy':'exclude'})
    assert np.load(core/'core_index_per_bar.npy').tolist()==[0,-1]
    assert np.load(core/'valid_barangay_mask.npy').tolist()==[True,False]
    assert json.loads((core/'metadata.json').read_text())['unmodeled_population']==5
    assert '0128010002' in (core/'snap_audit.csv').read_text()
    again,again_key=route.points_core(workspace,workspace/'variants'/'population_weighted',rows,
              {'data_file_sha256':'populationhash','source':{'units_sha256':'unitshash'}},pts,
              {'positions':np.array([[120.,14.],[120.1,14.]]),
               'max_snap_km':25.,'unsnappable_policy':'exclude'})
    assert again==core and again_key==key


def test_filter_origin_rows_preserves_missing_population_and_alignment():
    from net_distance_ph.population_weighted import filter_origin_rows,parse_args
    records=[{'psgc_code':'1380610004','population':100},
             {'psgc_code':'1380610005','population':200},
             {'psgc_code':'1380610006','population':300}]
    available={'1380610004':(120.2,14.2),'1380610006':(120.3,14.3)}
    included,positions,missing=filter_origin_rows(records,available)
    assert [r['psgc_code'] for r in included]==['1380610004','1380610006']
    assert positions.shape==(2,2)
    assert np.allclose(positions,[(120.2,14.2),(120.3,14.3)])
    assert sum(r['population'] for r in included)==400
    assert sum(r['population'] for r in missing)==200
    assert sum(r['population'] for r in records)==600
    assert parse_args([]).missing_geometry_policy=='exclude'
    assert parse_args([]).geometry_source=='latest'


def test_filter_origin_rows_errors_if_everything_missing_or_strict():
    from net_distance_ph.population_weighted import filter_origin_rows
    records=[{'psgc_code':'1380610004','population':10},
             {'psgc_code':'1380610005','population':20}]
    with pytest.raises(ValueError,match='No positive-population barangays have usable geometry'):
        filter_origin_rows(records,{})
    with pytest.raises(ValueError,match='1 positive-population barangays lack geometry'):
        filter_origin_rows(records,{'1380610004':(120,14)},on_missing='error')


def test_missing_geometry_whole_town_kept_in_output_denominator(tmp_path):
    from net_distance_ph.population_routing import _save_level
    from net_distance_ph.population_weighted import SCHEMA
    town_a,town_b='1380600000','1380700000'
    units=[{'code':town_a,'name':'Manila'}, {'code':town_b,'name':'Marikina'}]
    # 100 represented in Manila; Marikina's 900 residents have no geometry.
    town_num=np.array([[0.,0.],[0.,0.]])
    town_den=np.array([[10000.,0.],[0.,0.]])
    sites=[{'town_psgc':town_a,'population':100.,'mean_access_km':0.,
            'original_node':3,'core_node':2,'lon':120.,'lat':14.,'component':0}]
    extra={'source':{'population_total':1000},'population_total':1000,
           'missing_geometry_barangays':1,'missing_geometry_population':900,
           'missing_geometry_audit_csv':'/audit.csv'}
    home=tmp_path/'variant'
    out=_save_level(home,'town',units,np.arange(2),town_num,town_den,
        np.array([100.,0.]),sites,extra,route_hash='5'*64,
        core_key='6'*64,full_town_pop=np.array([100.,900.]),town_codes=[town_a,town_b])
    probability=np.load(out/'reachable_pair_probability.npy')
    distance=np.load(out/'distance_matrix_km.npy')
    assert probability[0,0]==1
    assert probability[1,1]==0 and probability[0,1]==0
    assert np.isinf(distance[1,1]) and np.isinf(distance[0,1])
    assert np.load(out/'population_per_unit.npy').tolist()==[100,900]
    assert np.load(out/'modeled_population_per_unit.npy').tolist()==[100,0]
    import json
    manifest=json.loads((out/'manifest.json').read_text())
    assert manifest['missing_geometry_population']==900
    assert manifest['unmodeled_origin_population']==900


def test_run_skips_missing_but_retains_unmodeled_town(tmp_path, monkeypatch):
    """Small integration of the actual orchestrator, without national graph I/O."""
    from net_distance_ph import population_routing as routing
    from net_distance_ph.population_weighted import parse_args
    ws=tmp_path/'ws'
    ds=tmp_path/'dataset'; ds.mkdir()
    town_a,town_b='1380600000','1380700000'
    bar_a,bar_b='1380610004','1380710001'
    region='1300000000'
    units={
        region:{'level':'region','name':'NCR','region_psgc':region,'province_psgc':'','town_psgc':'','geographic_level':'Reg'},
        town_a:{'level':'city','name':'Manila','region_psgc':region,'province_psgc':'','town_psgc':town_a,'geographic_level':'City'},
        town_b:{'level':'city','name':'Marikina','region_psgc':region,'province_psgc':'','town_psgc':town_b,'geographic_level':'City'},
    }
    people=[{'psgc_code':bar_a,'population':100,'town_psgc':town_a,'barangay_name':'a'},
            {'psgc_code':bar_b,'population':900,'town_psgc':town_b,'barangay_name':'b'}]
    from net_distance_ph.population_data import sha256
    points=ds/'barangay_points.csv'
    points.write_text(f'psgc_code,lon,lat\n{bar_a},120.1,14.6\n')
    (ds/'units.csv').write_text('units\n')
    (ds/'barangay_population.csv').write_text('population\n')
    popmeta={'population_total':1000,'data_file_sha256':sha256(ds/'barangay_population.csv'),
             'source':{'population_total':1000,'units_sha256':sha256(ds/'units.csv')}}
    monkeypatch.setattr(routing,'load_plain_dataset',lambda *a,**kw:(units,people,popmeta))
    monkeypatch.setattr('net_distance_ph.population_geometry.discover_points',
        lambda *a,**kw:points)
    def core(_,home,prepared,meta,points_file,params):
        assert len(prepared)==1 and prepared[0]['psgc_code']==bar_a
        assert params['positions'].shape==(1,2)
        assert meta['missing_geometry_population']==900
        return home/'core','1'*64
    monkeypatch.setattr(routing,'points_core',core)
    monkeypatch.setattr(routing,'build_town_sites',lambda *a:[{
        'town_psgc':town_a,'original_node':12,'core_node':8,'lon':120.1,'lat':14.6,
        'population':100.,'mean_access_km':0.,'component':0}])
    def routes(*a,**kw):
        assert kw['all_towns']==[town_a,town_b]
        return [town_a,town_b],np.zeros((2,2)),np.array([[10000,0],[0,0]]), '2'*64
    monkeypatch.setattr(routing,'compute_routing',routes)
    monkeypatch.setattr(routing,'verify_original_graph',lambda *a,**kw:{'checked':0})
    args=parse_args(['--workspace',str(ws),'--dataset-dir',str(ds),'--level','town','--verify-sites','0'])
    assert routing.run(args)==0
    result=Path(json.loads((ws/'variants/population_weighted/output/latest_run.json').read_text(encoding='utf-8'))['directory'])/'town'
    assert result.is_dir()
    assert np.load(result/'population_per_unit.npy').tolist()==[100,900]
    assert np.load(result/'modeled_population_per_unit.npy').tolist()==[100,0]
    assert np.load(result/'origin_population_coverage.npy').tolist()==[1,0]
    assert np.load(result/'reachable_pair_probability.npy')[1,0]==0
    audit=list((ws/'variants/population_weighted/geometry').glob('missing_origins_*.csv'))
    assert len(audit)==1 and bar_b in audit[0].read_text()
    manifest=json.loads((result/'manifest.json').read_text())
    assert manifest['missing_geometry_barangays']==1
    assert manifest['missing_geometry_population']==900

# Adaptive component-wise k-median selection (v4.3)

def test_adaptive_balanced_selects_multiple_medians_on_connected_town():
    x=np.arange(12.,dtype=float)
    D=np.abs(x[:,None]-x[None,:])
    selected,weights,access,mean,info=pop.select_sites_balanced(
        D,np.ones(len(x)),np.zeros(len(x)),max_sites=12)
    assert 1 < len(selected) < len(x)
    assert sum(weights)==12
    assert info['n_components']==1
    assert info['component_site_counts']==[len(selected)]
    assert 0 < info['selection_score'] < 1
    assert np.isfinite(access).all()
    assert mean < pop.select_sites(D,np.ones(len(x)),np.zeros(len(x)))[-1]
    assert np.isclose(info['selection_score'],
            info['normalized_road_access']+info['normalized_extra_sites'])


def test_adaptive_preserves_disconnected_components_and_site_cap():
    D=np.array([[0,2,4,np.inf,np.inf],
                [2,0,2,np.inf,np.inf],
                [4,2,0,np.inf,np.inf],
                [np.inf,np.inf,np.inf,0,6],
                [np.inf,np.inf,np.inf,6,0]],float)
    p=[10.,20.,30.,40.,50.]
    one,_,_,_,info_one=pop.select_sites_balanced(D,p,[0]*5,max_sites=2)
    more,weights,_,_,info=pop.select_sites_balanced(D,p,[0]*5,max_sites=5)
    assert len(one)==2
    assert len(more)==3
    assert info['component_site_counts']==[1,2]
    assert np.isclose(sum(weights),150)
    assert info['selection_score']<info_one['selection_score']
    for node in range(5):
        assert np.isfinite(D[node,more]).any()
    with pytest.raises(ValueError,match='disconnected demand components'):
        pop.select_sites_balanced(D,p,[0]*5,max_sites=1)


def test_adaptive_reproducible_independent_of_origin_offsets():
    n=8
    D=np.abs(np.arange(n)[:,None]-np.arange(n)[None,:]).astype(float)
    p=np.array([5.,10.,30.,10.,5.,25.,10.,5.])
    r1=pop.select_sites_balanced(D,p,np.zeros(n),max_sites=6)
    r2=pop.select_sites_balanced(D,p,np.linspace(.5,2,n),max_sites=6)
    assert np.array_equal(r1[0],r2[0])
    assert np.array_equal(r1[1],r2[1])
    assert r2[3]>r1[3]
    for _ in range(3):
        assert np.array_equal(pop.select_sites_balanced(D,p,np.zeros(n),max_sites=6)[0],r1[0])


def test_adaptive_one_per_component_when_no_internal_distance():
    D=np.array([[0.,np.inf],[np.inf,0.]])
    chosen,weights,access,err,info=pop.select_sites_balanced(D,[7,13],[0.,0.],max_sites=2)
    assert chosen.tolist()==[0,1]
    assert weights.tolist()==[7,13]
    assert np.all(access==0) and err==0
    assert info['selection_score']==0
    assert info['normalized_extra_sites']==0


def test_adaptive_uses_same_exact_single_median_with_one_site_cap():
    coords=np.array([0.,3.,10.,15.])
    D=np.abs(coords[:,None]-coords[None,:])
    p=[1,100,1,1]
    one=pop.select_sites(D,p,[0,0,0,0],max_sites=1)
    balanced=pop.select_sites_balanced(D,p,[0,0,0,0],max_sites=1)
    assert np.array_equal(one[0],balanced[0])
    assert np.array_equal(one[1],balanced[1])
    assert np.isclose(one[-1],balanced[-2])


def test_adaptive_cli_defaults_and_legacy_mode_switch():
    args=pop.parse_args([])
    assert args.median_selection=='balanced'
    assert args.max_mean_access_km is None
    assert pop.parse_args(['--median-selection','one']).median_selection=='one'
    assert pop.parse_args(['--max-mean-access-km','10']).max_mean_access_km==10.


def test_adaptive_town_checkpoint_and_population_od_preserved(tmp_path,monkeypatch):
    """The new site selection changes only representatives, not OD aggregation."""
    from scipy.sparse import coo_matrix
    # Six road nodes in a line: all residents of the first town are connected.
    u=np.arange(5); v=u+1; w=np.ones(5)
    G=coo_matrix((np.r_[w,w],(np.r_[u,v],np.r_[v,u])),shape=(6,6)).tocsr()
    monkeypatch.setattr(route.mx,'load_core',lambda *a,**k:G,raising=False)
    core=tmp_path/'core';core.mkdir()
    np.save(core/'core_index_per_bar.npy',np.arange(6,dtype=np.int32))
    np.save(core/'original_index_per_bar.npy',np.arange(6,dtype=np.int32))
    np.save(core/'offset_per_bar.npy',np.zeros(6))
    np.save(core/'snapped_node_lonlat.npy',np.column_stack((np.arange(6)+120,np.full(6,14))))
    town='0128010000';other='0128020000'
    rows=[{'town_psgc':town,'population':10} for _ in range(5)]+[
         {'town_psgc':other,'population':10}]
    roster={town:{'geographic_level':'Mun','name':'A'},
            other:{'geographic_level':'Mun','name':'B'}}
    selection={'median_selection':'balanced','mode':'balanced',
               'max_mean_access_km':None,'max_sites_per_town':6}
    sites=route.build_town_sites(tmp_path,core,'testkey',rows,roster,{'selection':selection})
    assert len(sites)>2  # auto-k in a town with connected origins
    assert sum(x['population'] for x in sites)==60
    again=route.build_town_sites(tmp_path,core,'testkey',rows,roster,{'selection':selection})
    assert again==sites
    audit=list((tmp_path/'town_sites'/'testkey').glob('*/selection_audit.csv'))
    assert len(audit)==1
    assert 'component_site_counts' in audit[0].read_text()
    towns,nums,dens,_=route.compute_routing(tmp_path,core,sites,'testkey',workers=1)
    i,j=[towns.index(x) for x in (town,other)]
    assert np.isclose(dens[i,j],50*10)
    assert np.isclose(dens[j,i],50*10)
    d,r=pop.finish_means(nums,dens,[50,10] if towns==[town,other] else [10,50])
    assert np.isfinite(d[i,j]) and r[i,j]==1
