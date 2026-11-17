"""Role-based names: existing selected releases remain current and reproducible."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from net_distance_ph.source_names import canonical_name, accepted_source_names
from net_distance_ph import workspace


def _shapefile(folder: Path, filename: str):
    path = folder / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    for ext in ('.shp', '.shx', '.dbf'):
        path.with_suffix(ext).write_bytes(b'fixture')
    return path


def test_names_do_not_encode_road_class_or_boundary_level():
    assert canonical_name('roads') == 'roads.shp'
    assert canonical_name('ferries') == 'ferry_routes.shp'
    assert canonical_name('centroids') == 'locality_origins.shp'
    assert accepted_source_names('roads') == ('roads.shp', 'primsectertrunk_road.shp')
    assert accepted_source_names('ferries') == ('ferry_routes.shp', 'ferry_route_shape.shp')
    assert accepted_source_names('centroids') == ('locality_origins.shp', 'muncity_centroids.shp')
    with pytest.raises(KeyError):
        canonical_name('road_profile')


@pytest.mark.parametrize('old', [False, True])
def test_workspace_accepts_both_names_in_selected_release(tmp_path, old):
    data = tmp_path / 'data'
    suffix = 1 if old else 0
    tp = data / 'transport_releases' / 'test'
    bp = data / 'boundary_releases' / 'test'
    road = _shapefile(tp, accepted_source_names('roads')[suffix])
    ferry = _shapefile(tp, accepted_source_names('ferries')[suffix])
    point = _shapefile(bp, accepted_source_names('centroids')[suffix])
    transport = {'roads':road.relative_to(data).as_posix(), 'ferries':ferry.relative_to(data).as_posix()}
    boundaries = {'centroids':point.relative_to(data).as_posix()}
    (data / 'current_transport.json').write_text(json.dumps(transport), encoding='utf-8')
    (data / 'current_boundaries.json').write_text(json.dumps(boundaries), encoding='utf-8')
    snapshot = ((data / 'current_transport.json').read_bytes(),
                (data / 'current_boundaries.json').read_bytes())
    assert workspace._selected(data, 'current_transport.json', {'roads':'primsectertrunk_road.shp', 'ferries':'ferry_route_shape.shp'})
    assert workspace._selected(data, 'current_boundaries.json', {'centroids':'muncity_centroids.shp'})
    assert snapshot == ((data / 'current_transport.json').read_bytes(), (data / 'current_boundaries.json').read_bytes())


@pytest.mark.parametrize('old', [False, True])
def test_runner_resolves_source_pointer_in_either_naming_scheme(tmp_path, monkeypatch, old):
    import config
    import run
    data = tmp_path / 'data'
    tp = data / 'transport_releases' / 'release'
    road = _shapefile(tp, accepted_source_names('roads')[int(old)])
    ferry = _shapefile(tp, accepted_source_names('ferries')[int(old)])
    bp = data / 'boundary_releases' / 'release'
    point = _shapefile(bp, accepted_source_names('centroids')[int(old)])
    (data / 'current_transport.json').write_text(json.dumps({
        'roads':road.relative_to(data).as_posix(),'ferries':ferry.relative_to(data).as_posix()}))
    (data / 'current_boundaries.json').write_text(json.dumps({
        'centroids':point.relative_to(data).as_posix()}))
    monkeypatch.setattr(config, 'DATA', data)
    assert run._current_inputs() == {'roads': road, 'ferries': ferry, 'localities': point}


def test_malformed_selected_name_rejected(tmp_path):
    data = tmp_path / 'data'
    folder = data / 'transport_releases' / 'test'
    road = _shapefile(folder, 'footways.shp')
    (data / 'current_transport.json').write_text(json.dumps({'roads':road.relative_to(data).as_posix()}))
    with pytest.raises(RuntimeError, match='unexpected name'):
        workspace._selected(data, 'current_transport.json', {'roads':'primsectertrunk_road.shp'})


def test_standalone_canonical_names_without_pointer(tmp_path, monkeypatch):
    import config
    import run
    from net_distance_ph import workspace
    data = tmp_path / 'data'
    for key in ('roads','ferries','centroids'):
        _shapefile(data / 'manual', canonical_name(key))
    monkeypatch.setattr(config, 'DATA', data)
    assert run._current_inputs()['roads'].name == 'roads.shp'
    assert run._current_inputs()['localities'].name == 'locality_origins.shp'
    assert workspace._selected(data, 'current_transport.json',
                               {'roads':'primsectertrunk_road.shp','ferries':'ferry_route_shape.shp'})


def test_transport_builder_publishes_only_canonical_names(tmp_path):
    import argparse
    import shapefile
    from scripts.build_transport import build
    xml = tmp_path / 'mini.osm'
    xml.write_text('''<osm version="0.6"><node id="1" lon="120" lat="14"/>
<node id="2" lon="120.01" lat="14"/>
<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way>
<way id="20"><nd ref="1"/><nd ref="2"/><tag k="route" v="ferry"/></way>
</osm>''', encoding='utf-8')
    args = argparse.Namespace(pbf=xml, download=False, source_url=None, expected_sha256=None,
                              output=tmp_path / 'data', include_links=False,
                              include_motorways=False, allow_no_ferries=False, replace=False)
    selected = build(args)
    assert Path(selected['roads']).name == 'roads.shp'
    assert Path(selected['ferries']).name == 'ferry_routes.shp'
    assert len(shapefile.Reader(str(args.output / selected['roads']))) == 1
    assert len(shapefile.Reader(str(args.output / selected['ferries']))) == 1
    manifest = json.loads((args.output / selected['provenance']).read_text())
    assert 'roads.shp' in manifest and 'ferry_routes.shp' in manifest
    assert manifest['road_profile'] == 'comprehensive'
