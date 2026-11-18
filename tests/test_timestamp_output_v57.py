"""Output directory naming is human-readable; routing identity remains hash-based."""
import hashlib
import json
from datetime import datetime, timezone

import numpy as np

from net_distance_ph.output_selection import (
    create_timestamped_run_directory, valid_output_run_name, previous_geometry,
)
from net_distance_ph.geographic_audit import _resolve_run
from net_distance_ph.population_routing import _save_level


def test_timestamped_folder_is_sortable_and_same_second_is_unique(tmp_path):
    clock = datetime(2026, 11, 18, 15, 20, 30, tzinfo=timezone.utc)
    first = create_timestamped_run_directory(tmp_path, now=clock)
    second = create_timestamped_run_directory(tmp_path, now=clock)
    assert first.name == '2026-11-18_15-20-30'
    assert second.name == '2026-11-18_15-20-30-001'
    assert first.is_dir() and second.is_dir()
    assert valid_output_run_name(first.name, 'a' * 64)
    assert valid_output_run_name(second.name, 'a' * 64)
    assert valid_output_run_name('a' * 16, 'a' * 64)
    assert not valid_output_run_name('2026-11-18 invalid', 'a' * 64)


def test_date_run_is_accepted_by_audit_and_exact_geometry_reuse(tmp_path):
    ws = tmp_path / 'workspace'
    home = ws / 'variants' / 'population_weighted'
    run = home / 'output' / '2026-11-18_15-20-30'
    (run / 'town').mkdir(parents=True)
    routing_hash, core_key = 'a' * 64, 'b' * 64
    (run / 'town' / 'manifest.json').write_text(json.dumps({
        'level': 'town', 'routing_hash': routing_hash, 'core_cache_key': core_key,
    }))
    (home / 'output' / 'latest_run.json').write_text(json.dumps({
        'routing_hash': routing_hash, 'directory': str(run),
    }))
    geo_dir = home / 'geometry'
    geo_dir.mkdir()
    points = geo_dir / 'barangay_points_faeldon_test.csv'
    points.write_text('psgc_code,lon,lat\n0100000001,120,14\n')
    (home / 'cores' / core_key[:16]).mkdir(parents=True)
    (home / 'cores' / core_key[:16] / 'metadata.json').write_text(json.dumps({
        'cache_key': core_key,
        'points_file_sha256': {'.csv': hashlib.sha256(points.read_bytes()).hexdigest()},
    }))
    assert _resolve_run(ws)[0] == run.resolve()
    assert previous_geometry(tmp_path / 'repo', ws, home) == points


def test_matrix_writer_accepts_timestamp_destination(tmp_path):
    town = '0128010000'
    site = {'town_psgc': town, 'population': 10, 'mean_access_km': 0.0}
    out = create_timestamped_run_directory(tmp_path / 'output',
        now=datetime(2026, 11, 18, 15, 20, 30, tzinfo=timezone.utc))
    target = _save_level(tmp_path, 'town', [{'code':town,'name':'Example'}], np.array([0]),
        np.zeros((1,1)), np.array([[100.0]]), np.array([10.0]), [site],
        {'population_total':10,'source':{'schema':'test'}},
        route_hash='a'*64, core_key='b'*64, full_town_pop=np.array([10.0]),
        town_codes=[town], output_run=out)
    assert target == out / 'town'
    assert (target/'manifest.json').is_file()
    assert (target/'distance_matrix_km.csv').is_file()
    assert json.loads((target/'manifest.json').read_text())['routing_hash']=='a'*64
