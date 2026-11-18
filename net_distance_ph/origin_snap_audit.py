"""Read-only v52 sensitivity audit for population origins snapped to minor graph components.

Reconstruct the exact population origin alignment from its hashed geometry source.
Compare each origin's *saved* graph snap against the nearest road vertex in the
largest population-bearing component. Geometric alternatives are NOT road links,
authorization to move population, or evidence of actual accessibility.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import time

import numpy as np
from scipy.spatial import cKDTree

from . import geographic_audit as ga
from .connectivity import labels_for_graph
from .geo import geodesic_km, lonlat_to_ecef, chord_to_arc_km

SCHEMA = 'net-distance-ph/origin-snap-sensitivity/v52'


def geometry_matches(path, recorded_hashes, digest):
    """Require exact recorded input sidecar hashes. No inferred sources."""
    path = Path(path)
    if not path.is_file() or not recorded_hashes or not isinstance(recorded_hashes, dict):
        return False
    sidecars = ([path.with_suffix(ext) for ext in ('.shp', '.shx', '.dbf', '.prj', '.cpg')]
                if path.suffix.lower() == '.shp' else [path])
    actual = {p.suffix.lower(): digest(p) for p in sidecars if p.is_file()}
    return actual == recorded_hashes


def resolve_geometry(repo, workspace, meta, requested=None):
    """Find an existing, exact-hash geometry source. Never download or rebuild."""
    expected = meta.get('points_file_sha256')
    if not isinstance(expected, dict) or not expected:
        raise ValueError('Population core has no recorded geometry file hashes')
    if requested is not None:
        target = Path(requested).expanduser().resolve()
        candidates = [target]
    else:
        location = workspace / 'variants' / 'population_weighted' / 'geometry'
        candidates = sorted(location.glob('barangay_points_*.csv'))
        candidates.append(repo / 'reference' / 'population' / 'default' / 'barangay_points.csv')
    matching = [p for p in candidates if geometry_matches(p, expected, ga._sha256)]
    if not matching:
        msg = ('No cached geometry source matches the population core SHA-256. '
               'Use --points-file PATH for the exact source used by the run; '
               'the audit will verify its bytes before proceeding.')
        raise FileNotFoundError(msg)
    return matching[0]


def rank_population_components(sites, original_labels):
    """Ordered selected-population components and dominant original component."""
    mass = Counter()
    town_sets = defaultdict(set)
    for site in sites:
        node = int(site['original_node'])
        if node < 0 or node >= len(original_labels):
            raise ValueError('Representative original node out of range')
        comp = int(original_labels[node])
        population = int(site['population'])
        if population <= 0:
            raise ValueError('Representative has nonpositive population')
        mass[comp] += population
        town_sets[comp].add(site['town_psgc'])
    if not mass:
        raise ValueError('No representative components')
    main = sorted(mass, key=lambda c: (-mass[c], c))[0]
    ordered = sorted((c for c in mass if c != main), key=lambda c: (-mass[c], c))
    return main, ordered, mass, town_sets


def select_priority_components(ordered, ranks=None, top=15):
    """Ranks are one-based v50 priority ranks, by population descending."""
    if ranks is not None:
        if not ranks or len(set(ranks)) != len(ranks) or any(r < 1 or r > len(ordered) for r in ranks):
            raise ValueError(f'--ranks must be distinct integers in 1..{len(ordered)}')
        return [(r, ordered[r-1]) for r in ranks]
    if not 1 <= top <= len(ordered):
        raise ValueError(f'--top must be in 1..{len(ordered)}')
    return list(enumerate(ordered[:top], 1))


def nearest_main_roads(origins, road_coords, road_ids):
    """Nearest main-component road vertex by ECEF chord, with WGS84 distance."""
    origins = np.asarray(origins, dtype=float)
    road_coords = np.asarray(road_coords, dtype=float)
    road_ids = np.asarray(road_ids, dtype=np.int64)
    if origins.ndim != 2 or origins.shape[1] != 2 or road_coords.ndim != 2 or road_coords.shape[1] != 2:
        raise ValueError('Origin and graph coordinates must have shape Nx2')
    if not len(road_ids) or len(road_ids) != len(road_coords):
        raise ValueError('No valid main-component road nodes')
    if not np.isfinite(origins).all() or not np.isfinite(road_coords).all():
        raise ValueError('Coordinates must be finite')
    tree = cKDTree(lonlat_to_ecef(road_coords[:,0], road_coords[:,1]))
    chord, indices = tree.query(lonlat_to_ecef(origins[:,0], origins[:,1]), k=1)
    indices = np.atleast_1d(indices).astype(np.int64)
    alternatives = road_coords[indices]
    distance = np.asarray(geodesic_km(origins[:,0], origins[:,1], alternatives[:,0], alternatives[:,1]), dtype=float)
    return road_ids[indices], distance, np.asarray(chord_to_arc_km(chord), dtype=float)


def assess_origin(population, current_distance, alt_distance, max_snap_km, tolerance=0.001, *, screen_distance=None):
    """Individual counterfactual classification; no revision of assignments."""
    if population <= 0 or min(current_distance, alt_distance) < 0 or not all(
            math.isfinite(v) for v in (current_distance, alt_distance, max_snap_km)) or max_snap_km <= 0:
        raise ValueError('Invalid population or snap distances')
    delta = alt_distance - current_distance
    # Match graphbuild.snap_sources: eligibility uses ECEF chord-to-arc distance.
    # WGS84 length is separately reported as a real-world geometric distance.
    within = (alt_distance if screen_distance is None else screen_distance) <= max_snap_km
    return {
        'alternative_within_original_snap_radius': bool(within),
        'additional_snap_km': round(delta, 6),
        'alternative_closer_than_saved': bool(delta < -tolerance),
        'sensitivity_class': ('MAIN_ROAD_WITHIN_ORIGINAL_SNAP_RADIUS' if within
                              else 'MAIN_ROAD_OUTSIDE_ORIGINAL_SNAP_RADIUS'),
    }


def execute(args):
    """Analyze existing archives. Reads original national graph; writes reports only."""
    from .population_data import load_plain_dataset
    from .population_weighted import read_points, filter_origin_rows
    from .topology_diagnostics import _site_rows
    from .workspace import timed_step

    start = time.monotonic()
    workspace = Path(args.workspace or (Path.home() / 'Documents' / 'net-distance-ph')).expanduser().resolve()
    run, manifest, _, routing_hash = ga._resolve_run(workspace, args.run_dir)
    repo = Path(__file__).resolve().parents[1]
    all_units, hashes = ga._population_units(repo, manifest)
    dataset = repo / 'reference' / 'population' / 'default'
    roster, population_rows, pop_meta = load_plain_dataset(dataset)
    if pop_meta['source']['units_sha256'] != hashes['units_sha256'] or pop_meta['source']['population_sha256'] != hashes['population_sha256']:
        raise ValueError('Population tables differ from routing run')

    sites = _site_rows(run / 'representative_sites.csv')
    _, _, modeled, _ = ga._load_town_tables(run)
    core_key = manifest['core_cache_key']
    core = workspace / 'variants' / 'population_weighted' / 'cores' / core_key[:16]
    meta = ga._read_json(core / 'metadata.json')
    if (meta.get('cache_key') != core_key or meta.get('population_sha256') != hashes['population_sha256']
            or meta.get('units_sha256') != hashes['units_sha256']):
        raise ValueError('Population core metadata and run fingerprints disagree')
    geom = resolve_geometry(repo, workspace, meta, args.points_file)
    points = read_points(geom)
    aligned_rows, positions, missing = filter_origin_rows(population_rows, points, on_missing='exclude')
    original_node = np.load(core / 'original_index_per_bar.npy', mmap_mode='r')
    snap_km = np.load(core / 'offset_per_bar.npy', mmap_mode='r')
    valid = np.load(core / 'valid_barangay_mask.npy', mmap_mode='r')
    if len(aligned_rows) != len(original_node) or len(snap_km) != len(valid) or len(valid) != len(aligned_rows):
        raise ValueError('Geometry alignment does not match cached barangay arrays')
    if int(np.count_nonzero(~valid)) != meta.get('n_unmodeled_barangays'):
        raise ValueError('Unmodeled barangay count disagrees with core metadata')
    if len(missing) != len({r['psgc_code'] for r in population_rows if r['psgc_code'] not in points}):
        raise ValueError('Invalid missing-geometry accounting')
    original_graph = workspace / 'work' / 'graph_full.npz'
    graph_sha = ga._sha256(original_graph)
    if graph_sha != meta.get('national_graph_sha256'):
        raise ValueError('Stage-1 graph differs from population core input SHA-256')
    with np.load(workspace / 'work' / 'sources.npz', allow_pickle=False) as src:
        source_fingerprint = json.loads(str(src['input_manifest']))['fingerprint']
    if meta.get('graph_source_fingerprint') != source_fingerprint:
        raise ValueError('Stage-1 graph source fingerprint differs from population core')
    print('PROVENANCE: population, geometry, original graph, and core fingerprints agree', flush=True)

    with timed_step('recompute saved original graph connectivity for snapping audit'):
        with np.load(original_graph, allow_pickle=False) as graph:
            lonlat = graph['node_lonlat']
            flags = graph['flags']
            u, v = graph['u'], graph['v']
        _, labels = labels_for_graph(len(lonlat), u, v)
        del u, v
    if flags.shape[0] != len(labels) or flags.ndim != 2 or flags.shape[1] < 2:
        raise ValueError('Graph flags inconsistent with original network')
    main, ordered, masses, shared_towns = rank_population_components(sites, labels)
    selected = select_priority_components(ordered, ranks=args.ranks, top=args.top)
    selected_set = {comp for _,comp in selected}
    ranks = {comp:rank for rank,comp in selected}
    if np.any(valid & ((original_node < 0) | (original_node >= len(labels)))):
        raise ValueError('Saved snapped barangay references an invalid original graph node')
    valid_positions = np.flatnonzero(valid)
    origin_labels = labels[np.asarray(original_node[valid_positions], dtype=np.int64)]
    origin_mass = Counter()
    for idx, comp in zip(valid_positions, origin_labels):
        origin_mass[int(comp)] += int(aligned_rows[int(idx)]['population'])
    # Every populated component must conserve its mass between demand and sites.
    if origin_mass != masses:
        examples = [(c, origin_mass[c], masses[c]) for c in sorted(set(origin_mass)|set(masses)) if origin_mass[c] != masses[c]][:10]
        raise ValueError(f'Population component mass differs from k-median representatives: {examples}')
    if sum(origin_mass.values()) != int(round(float(np.sum(modeled)))):
        raise ValueError('Population mass differs from town output model totals')
    audit_indices = np.array([int(idx) for idx,c in zip(valid_positions,origin_labels) if int(c) in selected_set], dtype=np.int64)
    main_road_ids = np.flatnonzero((labels == main) & flags[:,0])
    if not len(main_road_ids):
        raise ValueError('Main population component contains no road nodes')
    with timed_step(f'nearest main-component roads for {len(audit_indices):,} original population origins'):
        near_nodes, near_km, near_screen = nearest_main_roads(positions[audit_indices], lonlat[main_road_ids], main_road_ids)
    max_snap = float(meta['max_snap_km'])
    town_names = {code:r['name'] for code,r in all_units.items()}
    rows = []
    by_component = defaultdict(list)
    near_main = 0
    for i,alternative,dist,screen in zip(audit_indices, near_nodes, near_km, near_screen):
        record = aligned_rows[int(i)]
        node = int(original_node[int(i)])
        comp = int(labels[node])
        offset = float(snap_km[int(i)])
        if node == alternative and comp != main:
            raise AssertionError('Alternative main road node unexpectedly belongs to disconnected component')
        audit = assess_origin(int(record['population']), offset, float(dist), max_snap, screen_distance=float(screen))
        lon,lat = positions[int(i)]
        row = {'component_rank':ranks[comp], 'original_component':comp,
             'barangay_psgc':record['psgc_code'], 'barangay_name':record['barangay_name'],
             'town_psgc':record['town_psgc'], 'town_name':town_names[record['town_psgc']],
             'population':int(record['population']),
             'origin_lon':float(lon), 'origin_lat':float(lat),
             'current_node':node, 'current_node_road':bool(flags[node,0]),
             'current_node_ferry':bool(flags[node,1]),
             'current_node_lon':float(lonlat[node,0]),'current_node_lat':float(lonlat[node,1]),
             'current_snap_km':round(offset,6),
             'nearest_main_road_node':int(alternative),
             'nearest_main_road_lon':float(lonlat[alternative,0]),
             'nearest_main_road_lat':float(lonlat[alternative,1]),
             'nearest_main_road_km':round(float(dist),6),
             'nearest_main_road_snap_screen_km':round(float(screen),6),
             **audit}
        rows.append(row)
        by_component[comp].append(row)
        if audit['alternative_within_original_snap_radius']:
            near_main += row['population']
    rows.sort(key=lambda r: (r['component_rank'],-r['population'],r['barangay_psgc']))
    columns = ['component_rank','original_component','barangay_psgc','barangay_name','town_psgc','town_name',
               'population','origin_lon','origin_lat','current_node','current_node_road','current_node_ferry',
               'current_node_lon','current_node_lat','current_snap_km',
               'nearest_main_road_node','nearest_main_road_lon','nearest_main_road_lat','nearest_main_road_km',
               'alternative_within_original_snap_radius','additional_snap_km','alternative_closer_than_saved','sensitivity_class']
    summary = []
    for rank,comp in selected:
        items = by_component[comp]
        mass = sum(r['population'] for r in items)
        within_pop = sum(r['population'] for r in items if r['alternative_within_original_snap_radius'])
        summary.append({'priority_rank':rank,'original_component':comp,
                        'population':mass, 'representative_population':masses[comp],
                        'barangays':len(items),'towns':len(shared_towns[comp]),
                        'town_names':'; '.join(town_names[t] for t in sorted(shared_towns[comp])[:12]),
                        'population_with_main_road_within_run_radius':within_pop,
                        'share_with_main_road_within_run_radius':round(within_pop/mass,7) if mass else 0,
                        'population_with_main_road_outside_run_radius':mass-within_pop,
                        'population_where_main_road_closer_than_saved':sum(r['population'] for r in items if r['alternative_closer_than_saved']),
                        'nearest_main_road_km_min':min((r['nearest_main_road_km'] for r in items),default=None),
                        'nearest_main_road_km_max':max((r['nearest_main_road_km'] for r in items),default=None)})
    out = workspace / 'output' / 'origin_snap_sensitivity' / routing_hash[:16]
    out.mkdir(parents=True, exist_ok=True)
    ga._write_csv(out / 'affected_barangay_origins.csv',rows,columns)
    ga._write_csv(out / 'component_sensitivity.csv',summary,list(summary[0]) if summary else [
         'priority_rank','original_component','population','representative_population'])
    report = {'schema':SCHEMA, 'routing_hash':routing_hash,
       'stage1_graph_sha256':graph_sha, 'stage1_source_fingerprint':source_fingerprint,
       'population_input_hashes':hashes, 'geometry_source':str(geom),
       'geometry_file_sha256':meta['points_file_sha256'],
       'saved_max_snap_km':max_snap,
       'dominant_original_component':main,'dominant_population':masses[main],
       'selected_priority_ranks':[rank for rank,_ in selected],
       'selected_components':len(selected),'selected_modeled_population':int(sum(masses[c] for _,c in selected)),
       'barangay_origins_examined':len(rows),
       'population_with_main_road_within_original_snap_radius':near_main,
       'population_with_main_road_outside_original_snap_radius':int(sum(masses[c] for _,c in selected)-near_main),
       'population_for_which_main_road_is_closer':sum(r['population'] for r in rows if r['alternative_closer_than_saved']),
       'all_modeled_original_component_mass_verified':True,
       'source_population_coverage':int(sum(origin_mass.values())),
       'elapsed_seconds':round(time.monotonic()-start,3),
       'files':{'affected_origins':'affected_barangay_origins.csv','component_summary':'component_sensitivity.csv'},
       'interpretation':{
          'alternative':'Nearest vertex in the main populated component that was marked road, by ECEF chord ranking, WGS84 geodesic length; not a network path',
          'existing':'Exact population-origin snap assignment and offset, from saved original run; no resnapping was performed',
          'radius':'Exactly uses original run max_snap_km and the original graphbuild chord-to-arc screening metric, not evidence that moving the origin is valid',
          'nonconnection':'Original graph and selected k-medians remain unmodified; no links or population changes proposed',
          'nearest_road_caveat':'ECEF chord nearest-neighbour ranking may differ from strictly shortest WGS84 geodesic in near-ties',
          'scope':'Current report covers selected nonmain components, not all disconnected people unless --top covers every component',
          'counterfactual':'A reachable main-road vertex inside the original snap radius is a candidate for geographic review only; water, property access, or barriers may prohibit that attachment'
       }}
    ga._write_json(out/'origin_snap_sensitivity.json',report)
    print(f'ORIGINS: {len(rows):,} positive-population barangays in {len(selected)} selected components',flush=True)
    print(f'SENSITIVITY: {near_main:,}/{report["selected_modeled_population"]:,} residents have a main-component road vertex inside the original {max_snap:g} km snap radius',flush=True)
    print(f'SENSITIVITY REPORT: {out / "origin_snap_sensitivity.json"}',flush=True)
    return 0
