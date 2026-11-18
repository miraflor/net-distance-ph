"""Focused view API tests. No national files or external network required."""
from __future__ import annotations

import csv
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.request import urlopen

import numpy as np
import pytest

from net_distance_ph.view import ExplorerData, make_handler, _thin


def fixture_view(tmp_path):
    gpd = pytest.importorskip('geopandas')
    from shapely.geometry import Polygon
    import shapefile

    root = tmp_path / 'workspace'
    data = root / 'data'
    data.mkdir(parents=True)
    bundle = root / 'variants/population_weighted/output'
    runs = ['2026-11-18_20-51-09', '2026-11-19_08-00-00']
    unit_towns = [
        ('0100101000', 'A', 200), ('0100102000', 'B', 300), ('0200201000', 'C', 400),
    ]
    unit_provs = [('0100100000','Province 1',500),('0200200000','Province 2',400)]
    unit_regs = [('0100000000','Region 1',500),('0200000000','Region 2',400)]
    for run in runs:
        for level, rows in (('town',unit_towns),('province_equivalent',unit_provs),('region',unit_regs)):
            target = bundle / run / level
            target.mkdir(parents=True)
            with (target / 'units.csv').open('w',newline='',encoding='utf-8') as fd:
                w=csv.writer(fd);w.writerow(['row','psgc_code','name','population'])
                for i,(code,name,pop) in enumerate(rows):w.writerow([i,code,name,pop])
            n=len(rows)
            d=np.full((n,n),np.inf,dtype=float)
            np.fill_diagonal(d, 0.5)
            d[0,1]=d[1,0]=2.0
            np.save(target/'distance_matrix_km.npy',d)
            np.save(target/'reachable_pair_probability.npy',np.isfinite(d).astype(float))
            (target/'manifest.json').write_text(json.dumps({'level':level,'n_units':n}))
    (bundle/'latest_run.json').write_text(json.dumps({'directory':str(bundle/runs[-1])}))
    boundary = data/'boundary_releases/abcdef0123456789'
    boundary.mkdir(parents=True)
    polys = []
    for i in range(3):
        x=120+i
        polys.append(Polygon([(x,12),(x+.5,12),(x+.5,12.5),(x,12.5),(x,12)]))
    gdf=gpd.GeoDataFrame({'psgc_code':[v[0] for v in unit_towns],'name':[v[1] for v in unit_towns]},geometry=polys,crs='EPSG:4326')
    gdf.to_file(boundary/'municipality_boundaries.gpkg',layer='areas',driver='GPKG')
    (data/'current_boundaries.json').write_text(json.dumps({'boundaries':f'boundary_releases/{boundary.name}/municipality_boundaries.gpkg'}))
    transport = data/'transport_releases/0123456789abcdef'
    transport.mkdir(parents=True)
    def write_shp(file,rows):
        with shapefile.Writer(str(file),shapeType=shapefile.POLYLINE) as out:
            out.field('OSM_ID','C',size=24);out.field('OSM_CLASS','C',size=32)
            for i,(cls,line) in enumerate(rows):
                out.line([line]);out.record(str(i),cls)
    write_shp(transport/'roads.shp',[
        ('primary',[(120,12),(120.1,12.1)]),
        ('tertiary',[(121,12),(121.1,12.1)]),
        ('residential',[(122,12),(122.1,12.1)]),
    ])
    write_shp(transport/'ferry_routes.shp',[('ferry',[(120,12),(121,12)])])
    (data/'current_transport.json').write_text(json.dumps({'roads':f'transport_releases/{transport.name}/roads.shp','ferries':f'transport_releases/{transport.name}/ferry_routes.shp'}))
    ref=tmp_path/'reference_units.csv'
    with ref.open('w',encoding='utf-8',newline='') as fd:
        w=csv.writer(fd);w.writerow(['psgc_code','name','level','region_psgc','province_psgc','town_psgc'])
        w.writerow(['0100101000','A','municipality','0100000000','0100100000','0100101000'])
        w.writerow(['0100102000','B','municipality','0100000000','0100100000','0100102000'])
        w.writerow(['0200201000','C','municipality','0200000000','0200200000','0200201000'])
    return ExplorerData(root,reference_units=ref)


def test_catalog_geometry_network(tmp_path):
    data=fixture_view(tmp_path)
    c=data.catalog()
    assert len(c['runs'])==2
    assert c['latest']=='2026-11-19_08-00-00'
    run=c['latest']
    for level,expected in [('town',3),('province_equivalent',2),('region',2)]:
        m=data.metadata(run,level)
        assert m['n']==expected
        f=json.loads(data.geometry(run,level))
        assert len(f['features'])==expected
        assert all(f['geometry']['type'] in ('Polygon','MultiPolygon') for f in f['features'])
    roads=json.loads(data.network('roads'))
    assert len(roads['lines'])==2
    assert roads['counts']=={'primary':1,'tertiary':1}
    assert len(json.loads(data.network('ferries'))['lines'])==1
    with pytest.raises((ValueError,FileNotFoundError)):
        data.binary('../secret','town','distance_matrix_km.npy')


def test_http_endpoints(tmp_path):
    data=fixture_view(tmp_path)
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(data))
    t=threading.Thread(target=server.serve_forever,daemon=True)
    t.start()
    url=f'http://127.0.0.1:{server.server_address[1]}'
    try:
        def load(path):
            with urlopen(url+path,timeout=3) as response:
                return response.read(),response.headers.get('Content-Type')
        page,typ=load('/')
        assert b'NET DISTANCE PH' in page and 'text/html' in typ
        j,_=load('/api/catalog')
        run=json.loads(j)['latest']
        units,_=load(f'/api/metadata?run={run}&level=town')
        assert len(json.loads(units)['units'])==3
        arr,_=load(f'/api/matrix?run={run}&level=town')
        assert arr.startswith(b'\x93NUMPY')
        shapes,_=load(f'/api/geometry?run={run}&level=town')
        assert len(json.loads(shapes)['features'])==3
        route,_=load('/api/network?layer=ferries')
        assert len(json.loads(route)['lines'])==1
    finally:
        server.shutdown();server.server_close();t.join(timeout=3)


def test_prepared_cartography_cache_and_unmodified_sources(tmp_path, monkeypatch):
    """A `view` launch builds and then reuses cached display layers only."""
    data=fixture_view(tmp_path)
    boundary=data.data/'boundary_releases/abcdef0123456789/municipality_boundaries.gpkg'
    roads=data.data/'transport_releases/0123456789abcdef/roads.shp'
    before={p:(p.stat().st_size,p.stat().st_mtime_ns) for p in (boundary,roads)}
    data.prepare_assets()
    cached=list(data.view_cache.glob('*.gz'))
    assert len(cached)==5, cached  # three boundary levels, road and ferry
    assert len([p for p in cached if p.name.startswith('boundaries-')]) == 3
    assert all((p.stat().st_size > 0 for p in cached))
    assert before=={p:(p.stat().st_size,p.stat().st_mtime_ns) for p in (boundary,roads)}
    # A fresh process should not re-read the source GeoPackage when its exact
    # generalized display files already exist.
    import geopandas as gpd
    def refuse(*_a,**_kw):
        raise AssertionError('unexpected repeated GeoPackage read')
    monkeypatch.setattr(gpd,'read_file',refuse)
    data2=ExplorerData(data.root,reference_units=data.reference_units)
    data2.prepare_assets()
    assert json.loads(data2.geometry(data2.latest,'town'))['features'][0]['properties']['name']=='A'
    assert len(list(data.view_cache.glob('*.gz')))==5


def test_presentation_limits_highlights_to_top_x():
    source=Path(__file__).resolve().parents[1]/'net_distance_ph/view_app.html'
    text=source.read_text(encoding='utf-8')
    assert 'nearest.slice(0,x).map' in text
    assert 'nearest.map(z=>units[z.j].code)' not in text
    assert 'legendX' in text
    assert "Number.isFinite(dist(selected,idx))" in text
    assert '--land:#e9ecef' in text and '--reachable:#cfd7df' in text
    assert 'ctx.lineWidth=.55/scale' in text
