"""Offline tests for the 14-class default, access restrictions and cached upgrade."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts import build_transport as bt
from net_distance_ph import workspace as ws


COMPREHENSIVE = {
    "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "motorway", "service", "living_street",
    "trunk_link", "primary_link", "secondary_link", "tertiary_link", "motorway_link",
}
MAJOR = {"trunk", "primary", "secondary", "tertiary"}


def test_comprehensive_profile_has_exactly_fourteen_classes():
    assert len(COMPREHENSIVE) == 14
    assert set(bt.ROAD_CLASSES) == COMPREHENSIVE
    assert bt.resolved_road_classes("comprehensive") == COMPREHENSIVE
    assert bt.resolved_road_classes("major") == MAJOR
    assert bt.resolved_road_classes("major", include_motorways=True) == MAJOR | {"motorway", "motorway_link"}
    assert bt.resolved_road_classes("major", include_links=True) == MAJOR | {
        "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
    with pytest.raises(ValueError):
        bt.resolved_road_classes("random")


def test_motor_vehicle_restrictions_use_specificity():
    classes = bt.resolved_road_classes("comprehensive")
    def get(tags):
        return bt.way_kind(tags, 42, set(), classes)
    for hw in sorted(COMPREHENSIVE):
        assert get({"highway": hw}) == ("roads", hw)
        assert get({"highway": hw, "access": "private"}) is None
        assert get({"highway": hw, "motor_vehicle": "no"}) is None
    assert get({"highway": "service", "access": "private", "motorcar": "yes"}) == ("roads", "service")
    assert get({"highway": "residential", "access": "yes", "motorcar": "private"}) is None
    assert get({"highway": "service", "access": "customers"}) == ("roads", "service")
    assert get({"highway": "footway"}) is None
    assert get({"highway": "track"}) is None


ALL_WAYS = '''<osm version="0.6">
  <node id="1" lat="14" lon="120"/><node id="2" lat="14" lon="120.01"/>
  <node id="3" lat="14" lon="120.02"/><node id="4" lat="14" lon="120.03"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="trunk"/></way>
  <way id="20"><nd ref="2"/><nd ref="3"/><tag k="highway" v="unclassified"/></way>
  <way id="30"><nd ref="3"/><nd ref="4"/><tag k="highway" v="residential"/></way>
  <way id="40"><nd ref="3"/><nd ref="4"/><tag k="highway" v="motorway"/></way>
  <way id="50"><nd ref="3"/><nd ref="4"/><tag k="highway" v="motorway_link"/></way>
  <way id="60"><nd ref="3"/><nd ref="4"/><tag k="highway" v="service"/></way>
  <way id="70"><nd ref="3"/><nd ref="4"/><tag k="highway" v="service"/><tag k="access" v="private"/></way>
  <way id="80"><nd ref="3"/><nd ref="4"/><tag k="highway" v="living_street"/></way>
  <way id="90"><nd ref="3"/><nd ref="4"/><tag k="route" v="ferry"/></way>
</osm>'''


def test_builder_default_release_includes_local_motorway_service_and_keeps_way_class(tmp_path):
    import shapefile
    xml = tmp_path / "roads.osm"
    xml.write_text(ALL_WAYS, encoding="utf-8")
    args = argparse.Namespace(
        pbf=xml, download=False, source_url=None, expected_sha256=None,
        output=tmp_path / "data", include_links=False, include_motorways=False,
        allow_no_ferries=False, replace=False,
        # Deliberately omit road_profile: default used by previous API callers.
    )
    release = bt.build(args)
    provenance = json.loads((args.output / release["provenance"]).read_text(encoding="utf-8"))
    assert provenance["road_profile"] == "comprehensive"
    assert set(provenance["road_filter"]) == COMPREHENSIVE
    with shapefile.Reader(str(args.output / release["roads"])) as sf:
        roads = {(str(row[0]), str(row[1])) for row in sf.records()}
    assert roads == {(str(k), kind) for k, kind in [
        (10, "trunk"), (20, "unclassified"), (30, "residential"),
        (40, "motorway"), (50, "motorway_link"), (60, "service"),
        (80, "living_street"),
    ]}


def _release(root: Path, release_id: str, classes, sha: str = "a" * 64):
    folder = root / "transport_releases" / release_id
    folder.mkdir(parents=True, exist_ok=True)
    pointers = {}
    for key, filename in {"roads": "primsectertrunk_road.shp", "ferries": "ferry_route_shape.shp"}.items():
        path = folder / filename
        for suffix in (".shp", ".shx", ".dbf"):
            path.with_suffix(suffix).write_bytes(b"fixture")
        pointers[key] = path.relative_to(root).as_posix()
    prov = folder / "transport_provenance.json"
    prov.write_text(json.dumps({"road_filter": sorted(classes)}), encoding="utf-8")
    pointers.update({"release_id": release_id, "provenance": prov.relative_to(root).as_posix(),
                     "source_sha256": sha})
    (root / "current_transport.json").write_text(json.dumps(pointers), encoding="utf-8")
    return pointers


def _boundary(root: Path):
    folder = root / "boundary_releases" / "fixture"
    folder.mkdir(parents=True)
    cent = folder / "muncity_centroids.shp"
    for suffix in (".shp", ".shx", ".dbf"):
        cent.with_suffix(suffix).write_bytes(b"fixture")
    (root / "current_boundaries.json").write_text(json.dumps({"centroids": cent.relative_to(root).as_posix()}))


def test_existing_major_profile_is_migrated_once_and_old_work_is_archived(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    _boundary(root)
    previous = _release(root, "oldmajor", MAJOR)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "graph_full.npz").write_bytes(b"old graph")
    (tmp_path / "work" / "sources.npz").write_bytes(b"old sources")
    (tmp_path / "output" / "connectivity_audit").mkdir(parents=True)
    (tmp_path / "output" / "connectivity_audit" / "audit.csv").write_text("old")
    built = []
    def rebuilt(target, *, replace=False, selected=None):
        assert replace is True
        assert selected == previous
        built.append(True)
        _release(target, "newfull", COMPREHENSIVE)
    monkeypatch.setattr(ws, "_build_transport", rebuilt)
    monkeypatch.delenv("NET_DISTANCE_PH_ROAD_PROFILE", raising=False)
    ws.prepare_sources(root, heartbeat_seconds=1)
    assert built == [True]
    archived = tmp_path / "baselines" / "transport-oldmajor"
    assert (archived / "work" / "graph_full.npz").read_bytes() == b"old graph"
    assert (archived / "work" / "sources.npz").read_bytes() == b"old sources"
    assert (archived / "output" / "connectivity_audit" / "audit.csv").read_text() == "old"
    assert (root / previous["roads"]).exists()
    ws.prepare_sources(root, heartbeat_seconds=1)
    assert built == [True]


def test_failed_replacement_refuses_different_cached_pbf_and_keeps_pointer(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    record = _release(root, "oldmajor", MAJOR, sha="a" * 64)
    pbf = root / "sources" / "philippines-latest.osm.pbf"
    pbf.parent.mkdir()
    pbf.write_bytes(b"different source snapshot")
    original = (root / "current_transport.json").read_bytes()
    with pytest.raises(RuntimeError, match="Transport source preparation failed"):
        ws._build_transport(root, replace=True, selected=record)
    assert (root / "current_transport.json").read_bytes() == original


def test_missing_cached_pbf_never_downloads_on_migration(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    record = _release(root, "oldmajor", MAJOR)
    with pytest.raises(FileNotFoundError, match="Cached OSM PBF missing"):
        ws._build_transport(root, replace=True, selected=record)


def test_cli_accepts_profile_on_requested_stages():
    from net_distance_ph.cli import build_parser
    p = build_parser()
    assert p.parse_args(["run", "--until", "connectivity", "--road-profile", "comprehensive"]).road_profile == "comprehensive"
    assert p.parse_args(["graph", "--road-profile", "major"]).road_profile == "major"
    assert p.parse_args(["prepare"]).road_profile == "comprehensive"
