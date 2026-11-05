"""Offline OSM acquisition, route relations, and transactional release tests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.build_transport import FERRY_FILTER, build, way_kind, _xml_ways


# A ferry relation as mappers often record it (public-transport schema):
# the sea crossing (way 300, role ""), an approach road (way 600, tertiary,
# role ""), a terminal platform polygon (way 700, role "platform") and a pier
# outline (way 800, man_made=pier, role ""). Only way 300 is a sea crossing.
# Way 500 is a tolled expressway (highway=motorway) joined by a ramp (510).
RICH_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="synthetic-test">
  <node id="1" lat="14.0" lon="120.0"/><node id="2" lat="14.0" lon="120.1"/>
  <node id="3" lat="14.0" lon="121.0"/><node id="4" lat="14.0" lon="121.1"/>
  <node id="5" lat="14.2" lon="120.0"/><node id="6" lat="14.2" lon="120.1"/>
  <node id="7" lat="14.001" lon="120.101"/><node id="8" lat="14.001" lon="120.102"/>
  <node id="9" lat="14.002" lon="120.102"/>
  <node id="11" lat="13.999" lon="120.1005"/><node id="12" lat="13.998" lon="120.101"/>
  <way id="100"><nd ref="1"/><nd ref="2"/><tag k="highway" v="trunk"/></way>
  <way id="200"><nd ref="3"/><nd ref="4"/><tag k="highway" v="primary"/></way>
  <way id="300"><nd ref="2"/><nd ref="3"/></way>
  <way id="500"><nd ref="5"/><nd ref="6"/><tag k="highway" v="motorway"/></way>
  <way id="510"><nd ref="6"/><nd ref="2"/><tag k="highway" v="motorway_link"/></way>
  <way id="600"><nd ref="1"/><nd ref="5"/><tag k="highway" v="tertiary"/></way>
  <way id="700"><nd ref="7"/><nd ref="8"/><nd ref="9"/><nd ref="7"/>
    <tag k="public_transport" v="platform"/><tag k="area" v="yes"/></way>
  <way id="800"><nd ref="11"/><nd ref="12"/><tag k="man_made" v="pier"/></way>
  <relation id="900">
    <member type="way" ref="300" role=""/>
    <member type="way" ref="600" role=""/>
    <member type="way" ref="700" role="platform"/>
    <member type="way" ref="800" role=""/>
    <tag k="type" v="route"/><tag k="route" v="ferry"/>
  </relation>
</osm>
"""


def _extract_ids(release, root):
    import shapefile
    return {key: sorted(int(r[0]) for r in shapefile.Reader(str(root / release[key])).records())
            for key in ("roads", "ferries")}


OSM_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="synthetic-test">
  <node id="1" lat="14.0" lon="120.0"/>
  <node id="2" lat="14.0" lon="120.1"/>
  <node id="3" lat="14.0" lon="121.0"/>
  <node id="4" lat="14.0" lon="121.1"/>
  <way id="100"><nd ref="1"/><nd ref="2"/><tag k="highway" v="trunk"/></way>
  <way id="200"><nd ref="3"/><nd ref="4"/><tag k="highway" v="primary"/></way>
  <way id="300"><nd ref="2"/><nd ref="3"/></way>
  <way id="400"><nd ref="3"/><nd ref="4"/><tag k="highway" v="residential"/></way>
  <relation id="900">
    <member type="way" ref="300" role=""/>
    <tag k="type" v="route"/><tag k="route" v="ferry"/>
  </relation>
</osm>
"""


def _args(tmp_path, **overrides):
    osm = tmp_path / "fixture.osm"
    if not osm.exists():
        osm.write_text(OSM_FIXTURE, encoding="utf-8")
    return argparse.Namespace(**{
        "pbf": osm, "download": False, "source_url": None,
        "expected_sha256": None, "output": tmp_path / "data",
        "include_links": False, "allow_no_ferries": False,
        "replace": False, **overrides,
    })


def test_ferry_relation_selects_untagged_way(tmp_path):
    args = _args(tmp_path)
    ways = list(_xml_ways(args.pbf, {"trunk", "primary", "secondary", "tertiary"}))
    assert [(osm_id, selected) for osm_id, selected, _ in ways] == [
        (100, ("roads", "trunk")), (200, ("roads", "primary")),
        (300, ("ferries", "ferry")),
    ]
    assert way_kind({"highway": "secondary", "route": "ferry"}, 9, set(), {"secondary"}) == ("ferries", "ferry")


def test_release_is_consistent_and_selected_by_pointer(tmp_path):
    import shapefile
    args = _args(tmp_path)
    first = build(args)
    root = args.output
    selected = json.loads((root / "current_transport.json").read_text())
    assert selected == first
    road, ferry = root / selected["roads"], root / selected["ferries"]
    assert len(shapefile.Reader(str(road))) == 2
    assert len(shapefile.Reader(str(ferry))) == 1
    assert (road.with_suffix(".provenance.json")).exists()
    assert (ferry.with_suffix(".prj")).exists()
    manifest = json.loads((root / selected["provenance"]).read_text())
    assert manifest["extraction_counts"]["ferries"] == 1

    with pytest.raises(FileExistsError):
        build(args)
    assert (root / "current_transport.json").read_text() == json.dumps(first, indent=2) + "\n"

    newer = build(_args(tmp_path, replace=True))
    assert newer["release_id"] != first["release_id"]
    assert road.exists()    # prior release retained


def test_bad_source_keeps_previous_release(tmp_path):
    args = _args(tmp_path)
    build(args)
    pointer = args.output / "current_transport.json"
    snapshot = pointer.read_bytes()
    bad = tmp_path / "bad.osm"
    bad.write_text('<osm><node id="1" lon="120" lat="14"/></osm>', encoding="utf-8")
    with pytest.raises(ValueError, match="No selected road"):
        build(_args(tmp_path, pbf=bad, replace=True))
    assert pointer.read_bytes() == snapshot


def test_run_prefers_selected_transport_shapefiles(tmp_path, monkeypatch):
    import config
    import run
    args = _args(tmp_path)
    build(args)
    monkeypatch.setattr(config, "DATA", args.output)
    assert run._find(config.ROAD_SHP) == args.output / json.loads(
        (args.output / "current_transport.json").read_text())["roads"]


def test_missing_ferry_requires_explicit_override(tmp_path):
    osm = tmp_path / "land.osm"
    osm.write_text(OSM_FIXTURE.split('<relation')[0] + '</osm>', encoding='utf-8')
    # A missing ferry relation must never silently drop all sea links.
    with pytest.raises(ValueError, match="No ferry"):
        build(_args(tmp_path, pbf=osm))


def test_full_cli_from_independently_extracted_osm(tmp_path):
    """OSM XML -> selected network release -> origin snap -> matrix and verify."""
    from tests.mkshp import write_point_shp, write_dbf
    import numpy as np

    args = _args(tmp_path)
    build(args)
    cent = args.output / "user_origins" / "muncity_centroids.shp"
    cent.parent.mkdir(parents=True, exist_ok=True)
    write_point_shp(cent, [(120.0, 14.0), (121.1, 14.0)])
    write_dbf(cent.with_suffix(".dbf"),
              [("PSGC_CODE", "C", 10, 0), ("ADM3_EN", "C", 24, 0)],
              [("0100101000", "Island A"), ("0200201000", "Island B")])
    root = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        [sys.executable, str(root / "run.py"), "all", "--data", str(args.output),
         "--work", str(tmp_path / "work"), "--out", str(tmp_path / "out"),
         "--workers", "1"],
        cwd=root, capture_output=True, text=True, timeout=90,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    D = np.load(tmp_path / "out" / "distance_matrix_km.npy")
    assert D.shape == (2, 2) and np.isfinite(D[0, 1])
    assert D[0, 1] > 100
    assert "RESULT: all checks passed" in (tmp_path / "out" / "verification.txt").read_text()
    report = json.loads((tmp_path / "out" / "matrix_report.json").read_text())
    assert len(report["input_fingerprint"]) == 64


def test_pbf_relation_scan_with_osmium_like_api(tmp_path, monkeypatch):
    """Exercise both PBF passes against the pyosmium callback contract offline."""
    import sys
    import types
    from scripts.build_transport import _pbf_ways

    class Tags(list):
        pass

    class Ref:
        def __init__(self, ref, kind="w", role=""):
            self.ref, self.type, self.role = ref, kind, role

    class Location:
        def __init__(self, lon, lat):
            self.lon, self.lat = lon, lat

    class NodeRef:
        def __init__(self, lon, lat):
            self.location = Location(lon, lat)
            self.ref = int(lon * 1000 + lat)   # distinct node ID per coordinate here

    class SimpleHandler:
        def apply_file(self, path, locations=False, idx=None):
            if not locations:
                assert idx is None
                self.relation(types.SimpleNamespace(tags=[types.SimpleNamespace(k="type", v="route"),
                                      types.SimpleNamespace(k="route", v="ferry")],
                                      members=[Ref(30)]))
            else:
                assert idx == "flex_mem"
                for osm_id, tags, coords in (
                    (10, {"highway": "trunk"}, [(120, 14), (121, 14)]),
                    (30, {}, [(121, 14), (122, 14)]),
                ):
                    self.way(types.SimpleNamespace(
                        id=osm_id,
                        tags=[types.SimpleNamespace(k=k, v=v) for k, v in tags.items()],
                        nodes=[NodeRef(lon, lat) for lon, lat in coords],
                    ))

    monkeypatch.setitem(sys.modules, "osmium", types.SimpleNamespace(SimpleHandler=SimpleHandler))
    source = tmp_path / "example.pbf"
    source.write_bytes(b"fixture handled by fake callback")
    rows = []
    _pbf_ways(source, {"trunk"})(lambda osm_id, classification, xy: rows.append(
        (osm_id, classification, xy)))
    assert [(x[0], x[1]) for x in rows] == [(10, ("roads", "trunk")),
                                             (30, ("ferries", "ferry"))]
    assert rows[1][-1] == [(121.0, 14.0), (122.0, 14.0)]


def _rich_args(tmp_path, name="rich.osm", **overrides):
    osm = tmp_path / name
    if not osm.exists():
        osm.write_text(RICH_FIXTURE, encoding="utf-8")
    return _args(tmp_path, pbf=osm, **overrides)


def test_ferry_relation_members_that_are_not_the_crossing_are_excluded(tmp_path):
    """Approach roads stay roads; platforms and piers are not ferry routes.

    Earlier, every member way of a ferry relation became a ferry line: the
    platform polygon became a closed "route" ring and the tertiary approach
    road was moved out of the road layer.
    """
    args = _rich_args(tmp_path)
    release = build(args)
    assert _extract_ids(release, args.output) == {"roads": [100, 200, 600], "ferries": [300]}
    # Each file's provenance states its own filter.
    ferry_prov = json.loads((args.output / release["ferries"]).with_suffix(".provenance.json").read_text())
    road_prov = json.loads((args.output / release["roads"]).with_suffix(".provenance.json").read_text())
    assert ferry_prov["filter"] == FERRY_FILTER
    assert road_prov["filter"] == ["primary", "secondary", "tertiary", "trunk"]


def test_motorways_are_opt_in(tmp_path):
    args = _rich_args(tmp_path)
    assert 500 not in _extract_ids(build(args), args.output)["roads"]
    release = build(_rich_args(tmp_path, include_motorways=True, replace=True))
    assert {500, 510} <= set(_extract_ids(release, args.output)["roads"])
    manifest = json.loads((args.output / release["provenance"]).read_text())
    assert {"motorway", "motorway_link"} <= set(manifest["road_filter"])


def _to_pbf(osm_path, pbf_path):
    osmium = pytest.importorskip("osmium")
    with osmium.SimpleWriter(str(pbf_path)) as writer:
        for obj in osmium.FileProcessor(str(osm_path)):
            writer.add(obj)
    return pbf_path


def test_real_pyosmium_pbf_matches_xml(tmp_path):
    """The national path (PBF via pyosmium) must select what the XML path selects."""
    pytest.importorskip("osmium")
    xml_args = _rich_args(tmp_path, output=tmp_path / "xml")
    pbf = _to_pbf(xml_args.pbf, tmp_path / "rich.osm.pbf")
    from_xml = _extract_ids(build(xml_args), xml_args.output)
    pbf_args = _args(tmp_path, pbf=pbf, output=tmp_path / "pbf")
    from_pbf = _extract_ids(build(pbf_args), pbf_args.output)
    assert from_pbf == from_xml == {"roads": [100, 200, 600], "ferries": [300]}


def test_real_pyosmium_missing_node_gives_clear_error(tmp_path):
    """A clipped extract must stop with a message, not a pyosmium traceback."""
    pytest.importorskip("osmium")
    clipped = tmp_path / "clipped.osm"
    clipped.write_text(RICH_FIXTURE.replace('<node id="4" lat="14.0" lon="121.1"/>', ""), encoding="utf-8")
    pbf = _to_pbf(clipped, tmp_path / "clipped.osm.pbf")
    args = _args(tmp_path, pbf=pbf)
    with pytest.raises(ValueError, match="lack node coordinates"):
        build(args)
    assert not (args.output / "current_transport.json").exists()
