"""Synthetic geometry-repair contract tests; not a real national validation."""
import json
from pathlib import Path
import pytest
import geopandas as gpd
from shapely.geometry import Polygon, box
from net_distance_ph.geometry_repair import repair_polygons


def _frame(polys):
    return gpd.GeoDataFrame({'adm3_psgc':['1380600000']*len(polys),
                             'adm3_en':['City of Manila']*len(polys)},
                            geometry=polys, crs='EPSG:32651')


def test_valid_noop():
    f=_frame([box(0,0,10,10)])
    out, repairs=repair_polygons(f,id_column='adm3_psgc',name_column='adm3_en')
    assert not repairs and out.geometry.iloc[0].equals(f.geometry.iloc[0])


def test_ring_self_touch_area_preserved_and_recorded():
    invalid=Polygon([(0,0),(10,0),(10,10),(5,10),(5,5),(5,10),(0,10),(0,0)])
    assert not invalid.is_valid
    f=_frame([invalid])
    result, repairs=repair_polygons(f,id_column='adm3_psgc',name_column='adm3_en')
    assert result.geometry.iloc[0].is_valid
    assert result.geometry.iloc[0].geom_type in {'Polygon','MultiPolygon'}
    assert len(repairs)==1 and repairs[0]['source_code']=='1380600000'
    assert repairs[0]['original_wkb_sha256'] != repairs[0]['repaired_wkb_sha256']
    assert repairs[0]['relative_area_change']<1e-12
    assert not f.geometry.iloc[0].is_valid  # source frame untouched
    assert 'make_valid' in repairs[0]['repair_method']
    json.dumps(repairs)


def test_unsafe_bowtie_rejected():
    invalid=Polygon([(0,0),(10,10),(0,10),(10,0)])
    assert not invalid.is_valid
    with pytest.raises(ValueError,match='UNSAFE geometry'):
        repair_polygons(_frame([invalid]),id_column='adm3_psgc',name_column='adm3_en')


def test_spike_rejected_if_bbox_shrinks():
    invalid=Polygon([(0,0),(10,0),(10,10),(5,10),(5,12),(5,10),(0,10),(0,0)])
    with pytest.raises(ValueError,match='UNSAFE geometry'):
        repair_polygons(_frame([invalid]),id_column='adm3_psgc',name_column='adm3_en')


def test_metres_required():
    f=_frame([box(0,0,10,10)]).to_crs('EPSG:4326')
    with pytest.raises(ValueError,match='projected'):
        repair_polygons(f,id_column='adm3_psgc',name_column='adm3_en')


def test_town_validator_audits_repair_without_touching_source(tmp_path, monkeypatch):
    from net_distance_ph import boundaries
    rows = [{'psgc_code': f'{i+100:010d}', 'name': f'Town {i}', 'kind': 'municipality'} for i in range(1641)]
    rows.append({'psgc_code': '1380600000', 'name': 'City of Manila', 'kind': 'city'})
    path = tmp_path/'roster.json'
    path.write_text(json.dumps({'schema': boundaries.SCHEMA, 'source': {'publication_date':'2026-06-30'}, 'localities': rows}))
    from shapely.affinity import translate
    shapes = [translate(box(400_000, 1_500_000, 400_010, 1_500_010),
                        xoff=(i%50)*12, yoff=(i//50)*12) for i in range(1642)]
    invalid = Polygon([(0,0),(10,0),(10,10),(5,10),(5,5),(5,10),(0,10),(0,0)])
    shapes[878] = translate(invalid, xoff=400_000+878%50*12, yoff=1_500_000+878//50*12)
    f = gpd.GeoDataFrame({'adm3_psgc':[r['psgc_code'] for r in rows],
                          'adm3_en':[r['name'] for r in rows],
                          'geo_level':['City' if r['kind']=='city' else 'Mun' for r in rows]},
                         geometry=shapes,crs='EPSG:32651')
    source=tmp_path/'src.zip';source.write_bytes(b'test stub')
    monkeypatch.setattr(boundaries,'_read_shapefile',lambda _:f)
    fixed, report = boundaries.validate(source,path,expected_sha=boundaries.sha256(source))
    assert fixed.geometry.is_valid.all()
    assert len(fixed)==1642 and not f.geometry.iloc[878].is_valid
    assert [r['row'] for r in report['geometry_repairs']]==[878]
    assert report['manila_feature_count']==1
    json.dumps(report)


def test_city_level_validator_audits_repair(tmp_path, monkeypatch):
    from net_distance_ph import boundary_levels, boundaries
    rows=[{'psgc_code':f'{i+100:010d}', 'name':f'City {i}', 'province':''} for i in range(149)]
    shapes=[box(400000+(i%20)*20,1500000+(i//20)*20,
                400010+(i%20)*20,1500010+(i//20)*20) for i in range(149)]
    invalid=Polygon([(0,0),(10,0),(10,10),(5,10),(5,5),(5,10),(0,10),(0,0)])
    from shapely.affinity import translate
    shapes[50]=translate(invalid,xoff=400000+50%20*20,yoff=1500000+50//20*20)
    f=gpd.GeoDataFrame({'adm3_psgc':[x['psgc_code'] for x in rows],
                        'adm3_en':[x['name'] for x in rows],
                        'geo_level':['City']*149},geometry=shapes,crs='EPSG:32651')
    monkeypatch.setattr(boundary_levels,'_official',lambda level:(rows,tmp_path/'workbook.xlsx'))
    monkeypatch.setattr(boundaries,'_read_shapefile',lambda _:f)
    monkeypatch.setattr(boundaries,'sha256',lambda _: 'a'*64)
    archive=tmp_path/'city.zip';archive.write_bytes(b'fake')
    fixed, roster, report = boundary_levels.validate_level(archive,'city',expected_sha='a'*64)
    assert len(roster)==149 and len(fixed)==149
    assert fixed.geometry.is_valid.all()
    assert report['geometry_repairs'][0]['row']==50
