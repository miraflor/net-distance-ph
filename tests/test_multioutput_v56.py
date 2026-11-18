"""Regression tests for the consolidated v56 matrix invocation and explicit paths."""
import hashlib
import json
from pathlib import Path

import pytest

from net_distance_ph.population_weighted import parse_args
from net_distance_ph.output_selection import previous_geometry, output_manifest, print_output_locations


def test_default_is_all_three_matrices_balanced_and_previous_geometry():
    args = parse_args([])
    assert args.level == 'all'
    assert args.median_selection == 'balanced'
    assert args.geometry_source == 'latest'
    assert parse_args(['--level', 'town']).level == 'town'
    assert parse_args(['--level', 'province']).level == 'province'
    assert parse_args(['--level', 'region']).level == 'region'
    assert parse_args(['--geometry-source', 'ocha']).geometry_source == 'ocha'


def _fixture(tmp_path):
    repo = tmp_path / 'repo'
    workspace = tmp_path / 'workspace'
    home = workspace / 'variants' / 'population_weighted'
    output = home / 'output'
    routing_hash = 'a' * 64
    key = 'b' * 64
    run = output / routing_hash[:16]
    (run / 'town').mkdir(parents=True)
    (home / 'cores' / key[:16]).mkdir(parents=True)
    geo = home / 'geometry'
    geo.mkdir(parents=True)
    points = geo / 'barangay_points_faeldon_test.csv'
    points.write_text('psgc_code,lon,lat\n0100000001,120,14\n', encoding='utf-8')
    digest = hashlib.sha256(points.read_bytes()).hexdigest()
    (run / 'town' / 'manifest.json').write_text(json.dumps({'routing_hash': routing_hash, 'core_cache_key': key, 'level': 'town'}))
    (output / 'latest_run.json').write_text(json.dumps({'routing_hash': routing_hash, 'directory': str(run)}))
    (home / 'cores' / key[:16] / 'metadata.json').write_text(json.dumps({'cache_key': key, 'points_file_sha256': {'.csv': digest}}))
    return repo, workspace, home, points


def test_reuses_exact_geometry_and_refuses_mutations(tmp_path):
    repo, ws, home, points = _fixture(tmp_path)
    assert previous_geometry(repo, ws, home) == points
    points.write_text('changed\n', encoding='utf-8')
    with pytest.raises(FileNotFoundError, match='missing or changed'):
        previous_geometry(repo, ws, home)


def test_no_latest_falls_back_to_preexisting_new_run_policy(tmp_path):
    assert previous_geometry(tmp_path, tmp_path, tmp_path/'variants'/'population_weighted') is None


def test_refuses_unsafe_latest_pointer(tmp_path):
    repo, ws, home, points = _fixture(tmp_path)
    latest = home/'output'/'latest_run.json'
    record = json.loads(latest.read_text())
    record['directory'] = str(tmp_path/'elsewhere')
    latest.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='directory'):
        previous_geometry(repo, ws, home)


def test_reports_all_levels_with_actual_output_files(tmp_path, capsys):
    paths = {}
    for name in ('town', 'province_equivalent', 'region'):
        folder = tmp_path/name
        folder.mkdir()
        for file in ('distance_matrix_km.csv', 'distance_matrix_km.npy',
                     'reachable_pair_probability.npy', 'units.csv', 'manifest.json'):
            (folder/file).write_text('fixture')
        paths[name] = folder
    locations = output_manifest(paths)
    index = tmp_path/'latest_run.json'
    print_output_locations(locations, index)
    output = capsys.readouterr().out
    assert all(f'OUTPUT [{name.upper()}]:' in output for name in paths)
    assert 'DISTANCE CSV:' in output and 'REACHABILITY:' in output and 'LATEST OUTPUT INDEX:' in output
    assert set(locations) == set(paths)
    (paths['region']/'units.csv').unlink()
    with pytest.raises(FileNotFoundError, match='incomplete region'):
        output_manifest(paths)


def test_single_level_report_only_contains_requested_level(tmp_path):
    p = tmp_path/'town'
    p.mkdir()
    for file in ('distance_matrix_km.csv', 'distance_matrix_km.npy',
                 'reachable_pair_probability.npy', 'units.csv', 'manifest.json'):
        (p/file).touch()
    result = output_manifest({'town': p})
    assert list(result) == ['town']
