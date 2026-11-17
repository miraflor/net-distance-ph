"""Regression tests: inspector must honor the same vehicle-access rule as extraction."""
from net_distance_ph import osm_gap_inspection as inspect
from scripts.build_transport import public_motorcar_access


def _gap():
    return {"component":"C0008", "town_names":"Example", "a":(120.0,10.0), "b":(120.0002,10.0), "km":0.02}


def _roads(tags):
    return [
        {"way_id": 1, "highway": "residential", "nodes": [(1,120.,10.),(2,120.00005,10.)], "tags": {}, "near_gap_m":0.},
        {"way_id": 2, "highway": "residential", "nodes": [(2,120.00005,10.),(3,120.00015,10.)], "tags": tags, "near_gap_m":0.},
        {"way_id": 3, "highway": "primary", "nodes": [(3,120.00015,10.),(4,120.0002,10.)], "tags":{}, "near_gap_m":0.},
    ]


def _analyze(tags):
    return inspect.examine([_gap()], {"C0008":_roads(tags)}, {"residential","primary"},1000)


def test_private_way_of_already_selected_class_cannot_produce_path():
    observations, nearby, _ = _analyze({"access":"private"})
    got=observations[0]
    assert got['baseline_local_path'] is False
    assert got['verdict'] == 'NO_LOCAL_OSM_NODE_PATH_IN_TESTED_RADIUS'
    assert got['witness_way_ids'] == ''
    assert next(row for row in nearby if row['osm_way_id']==2)['scenario']=='excluded_access'


def test_no_prohibited_motor_vehicle_access_on_selected_class():
    for key,value in [('motor_vehicle','no'),('motorcar','private'),('vehicle','agricultural'),('access','forestry'),('access','delivery')]:
        obs,_,_= _analyze({key:value})
        assert obs[0]['baseline_local_path'] is False, (key,value)


def test_specific_motorcar_yes_overrides_general_private_access():
    tags={'access':'private', 'motorcar':'yes'}
    assert public_motorcar_access(tags)
    observations, nearby, _= _analyze(tags)
    assert observations[0]['baseline_local_path'] is True
    assert observations[0]['verdict']=='BASELINE_OSM_NODE_PATH_NEEDS_GRAPH_REVIEW'
    assert next(row for row in nearby if row['osm_way_id']==2)['scenario']=='included'


def test_private_motorcar_overrides_general_yes():
    observations,_,_=_analyze({'access':'yes','motorcar':'private'})
    assert observations[0]['baseline_local_path'] is False


def test_permissive_access_stays_included_with_flag():
    observations,nearby,_=_analyze({'access':'permissive'})
    assert observations[0]['baseline_local_path'] is True
    assert observations[0]['witness_access_caveat'] is True
    assert next(row for row in nearby if row['osm_way_id']==2)['scenario']=='included'
