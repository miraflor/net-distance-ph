"""Realistic OSM way-stream regression for absent access tags, no PBF needed."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest
from net_distance_ph import osm_gap_inspection as inspect
from scripts.build_transport import public_motorcar_access


def gap():
    return {"component":"C0008", "town_names":"Sample town", "a":(120.,10.), "b":(120.0002,10.), "km":0.020}


def _mock_scan(monkeypatch, middle_tags):
    class FakeWay:
        def __init__(self, wid, tags, nodes):
            self.id=wid
            self.tags=[NS(k=k,v=v) for k,v in tags.items()]
            self.nodes=[NS(ref=ref,location=NS(lon=lon,lat=lat)) for ref,lon,lat in nodes]
    samples=[
        FakeWay(1, {"highway":"residential"}, [(1,120.,10.),(2,120.00005,10.)]),
        FakeWay(2, {"highway":"residential",**middle_tags}, [(2,120.00005,10.),(3,120.00015,10.)]),
        FakeWay(3, {"highway":"primary"}, [(3,120.00015,10.),(4,120.0002,10.)]),
    ]
    class FakeHandler:
        def apply_file(self, file, *, locations, idx):
            assert locations and idx=='flex_mem'
            for w in samples:
                self.way(w)
    monkeypatch.setitem(sys.modules,'osmium',NS(SimpleHandler=FakeHandler))
    found,counts=inspect.extract_nearby_ways(Path('unused-test.osm.pbf'),[gap()],1000)
    return found['C0008']


def test_missing_access_keys_are_not_injected(monkeypatch):
    ways=_mock_scan(monkeypatch,{'access':'private'})
    mid=ways[1]
    assert mid['tags']['access']=='private'
    assert 'motorcar' not in mid['tags']
    assert 'motor_vehicle' not in mid['tags']
    assert 'vehicle' not in mid['tags']
    assert not public_motorcar_access(mid['tags'])


@pytest.mark.parametrize('denied', [
    {'access':'private'}, {'vehicle':'no'}, {'motor_vehicle':'private'},
    {'motorcar':'delivery'}, {'access':'agricultural'},
])
def test_original_osm_prohibited_road_breaks_path(monkeypatch,denied):
    ways=_mock_scan(monkeypatch,denied)
    observations, rows, _=inspect.examine([gap()], {'C0008':ways}, {'residential','primary'}, 1000)
    r=observations[0]
    assert r['baseline_local_path'] is False
    assert r['verdict']=='NO_LOCAL_OSM_NODE_PATH_IN_TESTED_RADIUS'
    assert r['witness_way_ids']==''
    assert next(x for x in rows if x['osm_way_id']==2)['scenario']=='excluded_access'


def test_specific_access_override_matches_builder(monkeypatch):
    ways=_mock_scan(monkeypatch, {'access':'private','motorcar':'yes'})
    assert public_motorcar_access(ways[1]['tags'])
    observations, rows, _=inspect.examine([gap()], {'C0008':ways}, {'residential','primary'}, 1000)
    assert observations[0]['baseline_local_path'] is True
    assert observations[0]['verdict']=='BASELINE_OSM_NODE_PATH_NEEDS_GRAPH_REVIEW'
    assert next(x for x in rows if x['osm_way_id']==2)['scenario']=='included'


def test_unrestricted_missing_access_tags_still_connect(monkeypatch):
    ways=_mock_scan(monkeypatch, {})
    observations, _, _=inspect.examine([gap()], {'C0008':ways}, {'residential','primary'}, 1000)
    assert observations[0]['baseline_local_path'] is True
