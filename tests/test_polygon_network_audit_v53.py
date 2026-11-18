"""v53 bounded polygon-network diagnostics; synthetic fixture never modifies repo data."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from net_distance_ph import polygon_network_audit as v53


def _make_polygon_archive(tmp_path,psgc=v53.SIX_PSGC, *, numeric_dbf=False):
    import shapefile
    from pyproj import CRS
    source=tmp_path/'zipped';source.mkdir()
    target=source/'bgy'
    with shapefile.Writer(str(target),shapeType=shapefile.POLYGON) as w:
        w.field('adm4_psgc', 'N' if numeric_dbf else 'C', size=10, decimal=0)
        for i,code in enumerate(psgc):
            lon=120+i*.02;lat=14
            # clockwise exterior expected by shapefile conventions
            ring=[[lon,lat],[lon,lat+.01],[lon+.01,lat+.01],[lon+.01,lat],[lon,lat]]
            w.poly([ring]);w.record(int(code) if numeric_dbf else code)
    (source/'bgy.prj').write_text(CRS.from_epsg(4326).to_wkt(),encoding='utf-8')
    archive=tmp_path/'archive.zip'
    with ZipFile(archive,'w') as z:
        for p in source.iterdir():z.write(p,arcname=p.name)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive,digest


def test_six_selection_fails_closed():
    rows=[{'barangay_psgc':c,'population':'1'} for c in v53.SIX_PSGC]
    assert len(v53.choose_six(rows))==6
    with pytest.raises(ValueError,match='missing required six'):
        v53.choose_six(rows[:-1])
    with pytest.raises(ValueError,match='Duplicate'):
        v53.choose_six(rows+[rows[-1]])
    with pytest.raises(ValueError,match='positive'):
        v53.choose_six(rows[:-1]+[dict(rows[-1],population='0')])


def test_pinned_polygons_and_provenance(tmp_path):
    archive,digest=_make_polygon_archive(tmp_path)
    with v53.pinned_barangay_polygons(archive,digest) as (polygons,verified):
        assert len(polygons)==6 and verified==digest
        assert all(poly.is_valid for poly,_,_ in polygons.values())
        assert polygons[v53.SIX_PSGC[0]][0].covers(Point(120.005,14.005))
    with pytest.raises(ValueError,match='SHA-256'):
        with v53.pinned_barangay_polygons(archive,'0'*64):pass


def test_numeric_dbf_restores_leading_zero_psgc(tmp_path):
    # PyShp's numeric DBF field stores 0331400014 as 331400014, etc.
    # The population reader already normalizes with code10(); v53 must too.
    archive,digest=_make_polygon_archive(tmp_path,numeric_dbf=True)
    with v53.pinned_barangay_polygons(archive,digest) as (polygons,verified):
        assert verified == digest
        assert set(polygons) == set(v53.SIX_PSGC)
        assert polygons['0331400014'][0].covers(Point(120.105,14.005))
        assert polygons['0402106056'][0].covers(Point(120.005,14.005))


def test_pinned_polygon_missing_row_refuses(tmp_path):
    archive,digest=_make_polygon_archive(tmp_path,v53.SIX_PSGC[:-1])
    with pytest.raises(ValueError,match='Missing polygon codes'):
        with v53.pinned_barangay_polygons(archive,digest):pass


def test_road_polygon_overlay_filters_ferries_and_detects_crossing():
    polygon=Polygon([(120.,14.),(120.01,14.),(120.01,14.01),(120.,14.01)])
    # Road 0 crosses polygon, endpoints OUTSIDE; road 1 intersects interior;
    # ferry 2 must never be included. Different components stay separate.
    coords=np.array([[119.995,14.005],[120.015,14.005],
                     [120.002,14.006],[120.008,14.006],
                     [120.003,14.002],[120.007,14.002]],float)
    edges_u=np.array([0,2,4]);edges_v=np.array([1,3,5])
    code=np.array([0,0,1]);labels=np.array([11,11,13,13,23,23])
    out=v53.road_overlay(polygon,(120.005,14.005),coords,edges_u,edges_v,code,labels,(11,13),chunk=2)
    assert out['component_edge_counts']=={11:1,13:1}
    assert out['component_lengths_m'][11]>1000
    assert out['component_lengths_m'][13]>500
    assert out['component_nearest_segment_m'][11]<.1
    assert out['component_nearest_segment_m'][13]>0


def test_road_polygon_no_roads_result():
    polygon=Polygon([(120.,14.),(120.01,14.),(120.01,14.01),(120.,14.01)])
    c=np.array([[120.02,14.02],[120.03,14.03]],float)
    got=v53.road_overlay(polygon,(120.005,14.005),c,np.array([0]),np.array([1]),np.array([0]),np.array([7,7]),(7,9))
    assert got['component_edge_counts']=={}
    assert got['component_nearest_segment_m'][9] is None


def test_road_polygon_component_violation_refused():
    polygon=Polygon([(120.,14.),(120.01,14.),(120.01,14.01),(120.,14.01)])
    c=np.array([[120.003,14.002],[120.007,14.002]],float)
    with pytest.raises(ValueError,match='links different'):
        v53.road_overlay(polygon,(120.005,14.005),c,np.array([0]),np.array([1]),np.array([0]),np.array([7,9]),(7,9))


def test_classification_descriptive():
    assert 'DIFFERENT_GRAPH_COMPONENTS' in v53.classify(True,True,True,True,False)
    assert 'ORIGIN_OUTSIDE' in v53.classify(True,True,False,True,True)
    assert 'MAIN_COMPONENT_ROADS' in v53.classify(False,True,True,False,True)
    assert 'NEITHER' in v53.classify(False,False,True,False,False)


def test_v51_access_gap_window_context(tmp_path):
    root=tmp_path/'output'/'topology_osm_investigation'/('f'*16)
    root.mkdir(parents=True)
    (root/'investigation_summary.json').write_text(json.dumps({'routing_hash':'f'*64,'status':'LOCAL_OSM_ID_EVIDENCE_ONLY'}))
    with (root/'nearby_osm_ways.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['component','osm_way_id','access','scenario']);w.writeheader()
        w.writerow({'component':'rank02_main','osm_way_id':'1','access':'private','scenario':'excluded_access'})
        w.writerow({'component':'rank02_main','osm_way_id':'1','access':'private','scenario':'excluded_access'})
        w.writerow({'component':'rank02_main','osm_way_id':'2','access':'','scenario':'included'})
        w.writerow({'component':'rank06_main','osm_way_id':'3','access':'private','scenario':'excluded_access'})
    result=v53._v51_access(tmp_path,'f'*64,2)
    assert result['restricted']==1 and result['private']==1
    assert v53._v51_access(tmp_path,'f'*64,3)['restricted']==0
    assert v53._v51_access(tmp_path,'0'*64,2) is None


def test_metric_projection_accurate_locally():
    xf=v53.metric_projection(120.,14.)
    x0,y0=xf.transform(120.,14.);x1,y1=xf.transform(120.001,14.)
    assert abs(x0)<.01 and abs(y0)<.01
    assert 100 < x1-x0 < 110


def test_polygon_overlays_never_alter_graph(tmp_path):
    coords=np.array([[120.002,14.005],[120.008,14.005]],float)
    orig=coords.copy();u=np.array([0],dtype=np.int32);v=np.array([1],dtype=np.int32)
    poly=Polygon([(120.,14.),(120.01,14.),(120.01,14.01),(120.,14.01)])
    v53.road_overlay(poly,(120.003,14.005),coords,u,v,np.array([0]),np.array([1,1]),(1,2))
    assert np.array_equal(orig,coords) and u.tolist()==[0] and v.tolist()==[1]


def test_synthetic_saved_run_end_to_end_v53(tmp_path, monkeypatch):
    """A complete v52->v53 fixture in tmp_path; never writes real workspace/repo."""
    from argparse import Namespace
    from net_distance_ph import geographic_audit as ga
    from net_distance_ph import boundary_levels, boundaries
    from net_distance_ph.connectivity import labels_for_graph

    root=tmp_path/'workspace'
    route_hash='f'*64;key='e'*64
    output=root/'variants'/'population_weighted'/'output'
    run=output/route_hash[:16]
    for d in (run/'town',run/'province',root/'work'):
        d.mkdir(parents=True)
    for level in ('town','province'):
        (run/level/'manifest.json').write_text(json.dumps({'routing_hash':route_hash,
            'core_cache_key':key,'level':level}))
    (output/'latest_run.json').write_text(json.dumps({'directory':str(run),'routing_hash':route_hash}))

    coords=[];edges_u=[];edges_v=[];codes=[];origin_xy=[];current_nodes=[];main_nodes=[]
    for i,psgc in enumerate(v53.SIX_PSGC):
        x=120.+i*.02;y=14.
        coords.extend([[x+.002,y+.005],[x+.004,y+.005],
                       [x+.006,y+.005],[x+.008,y+.005]])
        edges_u.extend([4*i,4*i+2]);edges_v.extend([4*i+1,4*i+3]);codes.extend([0,0])
        if i:
            edges_u.append(4*(i-1)+3);edges_v.append(4*i+2);codes.append(1)  # ferry only: join main component, not road overlay
        origin_xy.append((x+.005,y+.005));current_nodes.append(4*i);main_nodes.append(4*i+2)
    coords=np.asarray(coords)
    uu=np.asarray(edges_u,dtype=np.int32);vv=np.asarray(edges_v,dtype=np.int32);codes=np.asarray(codes,dtype=np.int8)
    graph=root/'work'/'graph_full.npz'
    np.savez(graph,u=uu,v=vv,code=codes,node_lonlat=coords)
    sha=ga._sha256(graph)
    _,labels=labels_for_graph(len(coords),uu,vv)
    dominant=int(labels[main_nodes[0]])
    assert all(int(labels[x])==dominant for x in main_nodes)
    assert all(int(labels[x])!=dominant for x in current_nodes)

    core=root/'variants'/'population_weighted'/'cores'/key[:16];core.mkdir(parents=True)
    points=root/'variants'/'population_weighted'/'geometry'/'barangay_points_faeldon_fixture.csv'
    points.parent.mkdir(parents=True)
    with points.open('w',newline='') as fp:
        w=csv.writer(fp);w.writerow(['psgc_code','lon','lat'])
        for code,xy in zip(v53.SIX_PSGC,origin_xy):w.writerow([code,*xy])
    point_hash={'.csv':ga._sha256(points)}
    (core/'metadata.json').write_text(json.dumps({'cache_key':key,
        'national_graph_sha256':sha,'points_file_sha256':point_hash}))
    archive,digest=_make_polygon_archive(tmp_path)
    monkeypatch.setitem(boundary_levels.ASSETS,'barangay',('archive.zip',digest))
    monkeypatch.setattr(boundaries,'RELEASE','fixture')
    archived=root/'data'/'sources'/'psgc-aligned'/'fixture'/'archive.zip'
    archived.parent.mkdir(parents=True)
    archived.write_bytes(archive.read_bytes())
    (points.with_suffix('.json')).write_text(json.dumps({'geometry_source_policy':'faeldon',
        'sources':[{'source':'faeldon/philippines-psgc-shapefiles','sha256':digest}]}))
    v52=root/'output'/'origin_snap_sensitivity'/route_hash[:16];v52.mkdir(parents=True)
    (v52/'origin_snap_sensitivity.json').write_text(json.dumps({'schema':'net-distance-ph/origin-snap-sensitivity/v52',
        'routing_hash':route_hash,'all_modeled_original_component_mass_verified':True,
        'stage1_graph_sha256':sha,'geometry_source':str(points),'geometry_file_sha256':point_hash,
        'dominant_original_component':dominant}))
    origin_rows=[]
    for i,code in enumerate(v53.SIX_PSGC):
        a=int(current_nodes[i]);b=int(main_nodes[i]);x,y=origin_xy[i]
        origin_rows.append({'barangay_psgc':code,'town_name':'Town','barangay_name':f'Synthetic {i}',
            'population':10,'component_rank':i+1,'origin_lon':x,'origin_lat':y,
            'current_node':a,'current_node_lon':coords[a,0],'current_node_lat':coords[a,1],
            'nearest_main_road_node':b,'nearest_main_road_lon':coords[b,0],
            'nearest_main_road_lat':coords[b,1],
            'original_component':int(labels[a]),'current_snap_km':.01,'nearest_main_road_km':.02,
            'additional_snap_km':.01})
    ga._write_csv(v52/'affected_barangay_origins.csv',origin_rows,list(origin_rows[0]))
    assert v53.execute(Namespace(workspace=root,run_dir=None))==0
    report=json.loads((root/'output'/'polygon_network_consistency'/route_hash[:16]/'polygon_network_consistency.json').read_text())
    assert report['total_residents_in_six']==60
    assert len(report['cases'])==6
    for item in report['cases']:
        assert item['origin_inside_polygon']
        assert item['current_snap_inside_polygon'] and item['main_alternative_inside_polygon']
        assert item['main_component_has_roads_in_polygon']
        assert item['current_component_has_roads_in_polygon']
        assert item['main_component_road_length_inside_m']>0
    geojson=json.loads((root/'output'/'polygon_network_consistency'/route_hash[:16]/'six_barangay_overlay.geojson').read_text())
    assert geojson['type']=='FeatureCollection' and len(geojson['features'])>=24
