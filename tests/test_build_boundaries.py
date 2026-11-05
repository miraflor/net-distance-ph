"""Offline tests for independent, fail-closed upstream boundary preparation."""
from __future__ import annotations

import argparse
import json

import pytest

# Every test here needs the optional [geo] extra (pandas, geopandas). The main
# CI job installs only [test], so skip there; the geo CI job runs them.
pytest.importorskip("geopandas")

import scripts.build_boundaries as bb  # noqa: E402
from scripts.build_boundaries import normalize_code, normalize_name, read_psgc, reconcile, build  # noqa: E402


def _psgc(tmp_path):
    path = tmp_path / 'psgc.csv'
    path.write_text('psgc_code,name,kind,province\n0100101000,San Juan,municipality,Abra\n0100102000,Quezon,municipality,Abra\n0200201000,San Juan,city,Cagayan\n', encoding='utf-8')
    return path


def _geoframe():
    import geopandas as gpd
    from shapely.geometry import Polygon
    return gpd.GeoDataFrame({
        'shapeID': ['PH0100101', 'fake-2', 'fake-3'],
        'shapeName': ['San Juan', 'Quezon', 'San Juan'],
        'ADM2_EN': ['Abra', 'Abra', 'Cagayan'],
    }, geometry=[Polygon([(120+i,14),(120.1+i,14),(120.1+i,14.1),(120+i,14.1)]) for i in range(3)], crs='EPSG:4326')


def test_psgc_parser_and_old_code(tmp_path):
    data = read_psgc(_psgc(tmp_path))
    assert data.psgc_code.tolist() == ['0100101000','0100102000','0200201000']
    assert normalize_code('PH0100101') == '0100101000'
    assert normalize_code('0100101000') == '0100101000'
    assert normalize_code('foo') is None
    assert normalize_name('City of San Juán') == 'san juan'


def test_reconciliation_code_then_province(tmp_path):
    roster = read_psgc(_psgc(tmp_path))
    matched, audit = reconcile(roster, _geoframe())
    assert [x[1] for x in matched] == ['0100101000','0100102000','0200201000']
    assert len([r for r in audit if r['status'] == 'matched']) == 3


def test_ambiguous_name_is_not_guessed(tmp_path):
    roster = read_psgc(_psgc(tmp_path))
    frame = _geoframe().drop(columns=['ADM2_EN'])
    frame.loc[2,'shapeID'] = 'no-code'
    matched, audit = reconcile(roster, frame)
    assert len(matched) == 2
    assert any(r['status']=='ambiguous' for r in audit)


def test_reviewed_overrides_are_explicit(tmp_path):
    roster = read_psgc(_psgc(tmp_path))
    frame = _geoframe().drop(columns=['ADM2_EN'])
    frame.loc[2,'shapeID']='no-code'
    matched, audit = reconcile(roster, frame, overrides={'no-code':'0200201000'})
    assert len(matched) == 3
    assert all(r['status']=='matched' for r in audit)


def _args(tmp_path, **changes):
    adm3 = tmp_path / 'adm3.geojson'
    if not adm3.exists():
        _geoframe().to_file(adm3, driver='GeoJSON')
    base = dict(psgc=_psgc(tmp_path), psgc_sheet=None, adm3=adm3, adm3_layer=None,
                adm2=None, adm2_layer=None, skip_adm2=True, adm2_province_col=None,
                boundary_id_col=None, boundary_code_col=None, boundary_name_col=None,
                boundary_province_col=None, overrides=None, output=tmp_path / 'output',
                point_method='centroid', replace=False, allow_extra_boundaries=False)
    return argparse.Namespace(**{**base, **changes})


def _published(output, key):
    pointer = json.loads((output / "current_boundaries.json").read_text(encoding="utf-8"))
    return output / pointer[key]


def _published_snapshot(output):
    pointer = output / "current_boundaries.json"
    current = json.loads(pointer.read_text(encoding="utf-8"))
    return {"pointer": pointer.read_bytes(), **{
        name: (output / current[name]).read_bytes()
        for name in ("boundaries", "centroids", "audit", "provenance")
    }}


def test_refused_rebuild_leaves_published_files_untouched(tmp_path):
    args = _args(tmp_path)
    assert build(args)
    published = _published_snapshot(args.output)
    with pytest.raises(FileExistsError):
        build(args)                                    # no --replace
    assert _published_snapshot(args.output) == published


def test_incomplete_attempt_does_not_overwrite_published_files(tmp_path):
    import geopandas as gpd
    args = _args(tmp_path)
    assert build(args)
    published = _published_snapshot(args.output)
    gpkg = _published(args.output, "boundaries").read_bytes()

    frame = gpd.read_file(args.adm3)
    frame.loc[1, 'shapeName'] = 'Unknown locality'
    changed = tmp_path / 'adm3_changed.geojson'
    frame.to_file(changed, driver='GeoJSON')
    assert not build(_args(tmp_path, adm3=changed, replace=True))

    assert _published_snapshot(args.output) == published
    assert _published(args.output, "boundaries").read_bytes() == gpkg
    attempt = args.output / bb.INCOMPLETE_DIR
    assert json.loads((attempt / 'boundary_provenance.json').read_text())['status'] == 'incomplete'
    assert (attempt / 'boundary_match_audit.csv').exists()

    assert build(_args(tmp_path, replace=True))     # a later success clears it
    assert not attempt.exists()


def test_cached_geoboundaries_file_keeps_its_own_metadata(tmp_path, monkeypatch):
    folder = tmp_path / 'sources'
    folder.mkdir()
    (folder / 'geoboundaries_phl_adm3_simplified.geojson').write_text('{}')
    (folder / 'geoboundaries_phl_adm3_metadata.json').write_text(json.dumps({'buildDate': 'cached'}))

    import requests

    def no_network(*a, **k):
        raise AssertionError('the API must not be queried when file and metadata are cached')

    monkeypatch.setattr(requests, 'get', no_network)
    path, meta = bb._geoboundaries('ADM3', folder)
    assert path.name == 'geoboundaries_phl_adm3_simplified.geojson'
    assert meta == {'buildDate': 'cached'}


def test_end_to_end_offline_with_custom_sources(tmp_path):
    args = _args(tmp_path)
    output = args.output
    assert build(args)
    assert _published(output, "boundaries").exists()
    assert _published(output, "centroids").exists()
    provenance = json.loads(_published(output, "provenance").read_text())
    assert provenance['matched_count'] == 3 and provenance['status'] == 'complete'
    assert 'output_boundaries_sha256' in provenance
    assert not build_with_wrong_name(args)


def build_with_wrong_name(args):
    """On a fresh output, strict audit must block an ambiguous full-data publish."""
    import geopandas as gpd
    frame = gpd.read_file(args.adm3)
    frame.loc[1,'shapeName'] = 'Unknown locality'
    changed = args.adm3.with_name('adm3_changed.geojson')
    frame.to_file(changed,driver='GeoJSON')
    args = argparse.Namespace(**{**vars(args), 'adm3':changed, 'output':args.output.with_name('incomplete')})
    return build(args)
