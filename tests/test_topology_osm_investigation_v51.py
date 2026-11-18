"""Offline tests for v51 population-gap OSM node-ID inspection helpers."""
import csv
import json
from pathlib import Path
import pytest
from net_distance_ph import topology_osm_investigation as t


def _priority(rank=2, towns="City of Dasmariñas (0402106000)"):
    return {"rank": str(rank), "towns": towns}


def _candidate(rank=2, relation="nearest_national_main_component", status="CANDIDATE_WITHIN_RADIUS",
               from_node="100", to_node="200"):
    return {"rank": str(rank), "relation": relation, "status": status,
            "source_node": from_node, "candidate_node": to_node,
            "source_lon": "120.1", "source_lat": "14.1", "candidate_lon": "120.2",
            "candidate_lat": "14.2", "geometric_gap_km": ".3",
            "core_component": "73", "original_component": "438",
            "modeled_population": "77355", "source_node_kind": "road",
            "candidate_node_kind": "road"}


def test_prefers_main_and_deduplicates_identical_node_pair():
    a = _candidate(relation="nearest_any_other_component")
    rows, skipped = t.select_gap_candidates([_priority()], [a, _candidate()], top=2)
    assert len(rows) == 1
    assert rows[0]["component"] == "rank02_main"
    assert rows[0]["modeled_population"] == 77355
    assert rows[0]["a"] == (120.1, 14.1)
    assert skipped == []


def test_different_nonmain_and_main_pairs_retained():
    a = _candidate(relation="nearest_any_other_component", to_node="201")
    rows, _ = t.select_gap_candidates([_priority()], [a, _candidate()], top=2)
    assert [r["component"] for r in rows] == ["rank02_main", "rank02_other"]


def test_relation_filter_and_no_candidate():
    a = _candidate(status="NO_OTHER_COMPONENT_WITHIN_SEARCH_WINDOW")
    b = _candidate(relation="nearest_any_other_component")
    rows, skipped = t.select_gap_candidates([_priority()], [a, b], top=2, relation="both")
    assert len(rows) == 1 and rows[0]["component"] == "rank02_other"
    assert len(skipped) == 1
    with pytest.raises(ValueError, match="No geometric gap"):
        t.select_gap_candidates([_priority()], [a, b], top=2, relation="main")


def test_rank_explicit_and_nonexistent_refused():
    a = _candidate()
    assert t.select_gap_candidates([_priority()], [a], ranks=(2,))[0][0]["priority_rank"] == 2
    with pytest.raises(ValueError, match="absent"):
        t.select_gap_candidates([_priority()], [a], ranks=(1,))


@pytest.mark.parametrize("kwargs,error", [({"top":0}, "--top"),({"relation":"ferry"}, "--relation"),
                                        ({"ranks":(0,)},"--ranks")])
def test_bad_selection_options(kwargs,error):
    with pytest.raises(ValueError, match=error):
        t.select_gap_candidates([_priority()], [_candidate()], **kwargs)


def test_invalid_coordinates_refused():
    a = _candidate();a["source_lon"] = "NaN"
    with pytest.raises(ValueError, match="Nonfinite"):
        t.select_gap_candidates([_priority()], [a], top=2)


def test_endpoint_osm_ids_preserve_distinct_nodes(monkeypatch):
    # The input rounder is the same as the graph's coordinate noder, but IDs
    # are deliberately kept separate rather than creating inferred junctions.
    from net_distance_ph import osm_gap_inspection as osm
    a = _candidate()
    gaps, _ = t.select_gap_candidates([_priority()], [a], top=2)
    way = {"way_id": 9, "highway": "residential", "tags": {"bridge":"yes"},
           "nodes": [(501, 120.1, 14.1), (502, 120.2, 14.2)]}
    other = {**way, "way_id": 10, "nodes": [(999, 120.1, 14.1), (502, 120.2, 14.2)]}
    rows = t.endpoint_node_identities(gaps, {"rank02_main":[way, other, other]})
    ids = {(r["side"], r["osm_node_id"], r["osm_way_id"]) for r in rows}
    assert ids == {("source",501,9),("source",999,10),("candidate",502,9),("candidate",502,10)}


def test_output_csv_empty_keeps_schema(tmp_path):
    p=tmp_path/"empty.csv"
    t._write_csv(p, [], ["code", "status"])
    assert p.read_text() == "code,status\n"


def test_graph_coordinate_provenance_guard(tmp_path):
    import numpy as np
    p=tmp_path/"graph_full.npz"
    np.savez(p,node_lonlat=np.array([[120.1,14.1],[120.2,14.2]]))
    a=_candidate(from_node="0",to_node="1")
    gaps,_=t.select_gap_candidates([_priority()], [a], top=2)
    t._validate_gap_coordinates(p, gaps)
    gaps[0]["b"]=(120.3,14.2)
    with pytest.raises(ValueError, match="differ"):
        t._validate_gap_coordinates(p, gaps)
