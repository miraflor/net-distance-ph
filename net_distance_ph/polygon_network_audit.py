"""v53: read-only, bounded polygon/road-network consistency check for six v52 origins.

Canonical GIS operations: verify source CRS/geometry, point-in-polygon, line/polygon
intersection, local projected line lengths, and point-to-road-segment distances.
Road classification is taken from the graph actually routed. A nearby road is
NOT evidence of legal or physical access; never modify topology or demand.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile

import numpy as np
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform
from pyproj import CRS, Transformer

from . import geographic_audit as ga
from . import origin_snap_audit as sa
from .population_data import code10
from .connectivity import labels_for_graph

SCHEMA = 'net-distance-ph/polygon-network-consistency/v53'
# Identified by the completed v52 audit, not dynamically chosen or promoted
# based on where a more connected candidate is available.
SIX_PSGC = (
    '0402106056',  # Paliparan III, Dasmariñas
    '1381000005',  # Santo Niño, Parañaque
    '0730600062',  # Punta Princesa, Cebu City
    '1380100181',  # Caloocan 181
    '1380100182',  # Caloocan 182
    '0331400014',  # New Cabalan, Olongapo
)
CSV_COLS = (
    'barangay_psgc','town_name','barangay_name','population','priority_rank',
    'source_polygon_valid','polygon_repaired_for_measurement',
    'origin_inside_polygon','current_snap_inside_polygon','main_alternative_inside_polygon',
    'current_snap_m','main_alternative_snap_m','additional_snap_m',
    'current_component','main_component',
    'eligible_road_components_intersecting_polygon','main_component_has_roads_in_polygon',
    'current_component_has_roads_in_polygon',
    'main_component_road_edges_intersecting_polygon','current_component_road_edges_intersecting_polygon',
    'main_component_road_length_inside_m','current_component_road_length_inside_m',
    'nearest_current_component_segment_inside_polygon_m','nearest_main_component_segment_inside_polygon_m',
    'v51_gap_window_access_restricted_ways','v51_gap_window_private_ways',
    'gis_observation',
)


def read_csv(path):
    with Path(path).open(newline='',encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def choose_six(rows, expected=SIX_PSGC):
    by_code = {}
    for r in rows:
        code=r.get('barangay_psgc')
        if code in expected:
            if code in by_code: raise ValueError(f'Duplicate v52 barangay {code}')
            by_code[code]=r
    if set(by_code)!=set(expected):
        raise ValueError(f'v52 affected origins missing required six: {sorted(set(expected)-set(by_code))}')
    if any(int(by_code[c]['population']) <= 0 for c in expected):
        raise ValueError('Audit records must have positive barangay population')
    return [by_code[c] for c in expected]


@contextmanager
def pinned_barangay_polygons(archive, expected_sha, selected=SIX_PSGC):
    """Read six shapes from pinned ZIP; no network access and no full 42k geometry load."""
    import shapefile
    from shapely.validation import explain_validity
    from shapely import make_valid
    archive=Path(archive).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f'Pinned barangay boundary archive absent (no download): {archive}')
    actual=ga._sha256(archive)
    if actual != expected_sha:
        raise ValueError('Barangay ZIP SHA-256 does not match the pinned source release')
    with tempfile.TemporaryDirectory(prefix='ndph_v53_polygons_') as temp:
        root=Path(temp)
        with ZipFile(archive) as zipf:
            files={Path(n).name.lower():n for n in zipf.namelist() if not n.endswith('/')}
            shp=[name for name in files if name.endswith('.shp')]
            if len(shp)!=1: raise ValueError('Pinned barangay archive must contain exactly one SHP')
            stem=Path(shp[0]).stem
            for ext in ('.shp','.shx','.dbf','.prj'):
                src=files.get(stem+ext)
                if not src: raise ValueError(f'Boundary ZIP missing {stem+ext}')
                with zipf.open(src) as srcfile, (root/(stem+ext)).open('wb') as dest:
                    shutil.copyfileobj(srcfile,dest)
        wkt=(root/(stem+'.prj')).read_text(encoding='utf-8-sig')
        transformer=Transformer.from_crs(CRS.from_wkt(wkt),CRS.from_epsg(4326),always_xy=True)
        with shapefile.Reader(str(root/(stem+'.shp'))) as reader:
            fields=[s[0].lower() for s in reader.fields[1:]]
            if 'adm4_psgc' not in fields:raise ValueError('Pinned ADM4 shapefile lacks adm4_psgc')
            code_col=fields.index('adm4_psgc')
            found={}
            for i,record in enumerate(reader.iterRecords()):
                code=code10(record[code_col])  # DBF may omit leading zeros
                if code not in selected:continue
                if code in found:raise ValueError(f'Duplicate polygon PSGC {code}')
                raw=shape(reader.shape(i).__geo_interface__)
                if raw.is_empty:raise ValueError(f'Empty polygon: {code}')
                original_valid=bool(raw.is_valid)
                if not original_valid:raw=make_valid(raw)
                if raw.is_empty or not raw.geom_type in ('Polygon','MultiPolygon','GeometryCollection'):
                    raise ValueError(f'Unusable polygon for {code}')
                poly=transform(transformer.transform,raw)
                if not poly.is_valid:poly=make_valid(poly)
                if poly.is_empty or not poly.area>0:raise ValueError(f'Invalid polygon geometry for {code}')
                found[code]=(poly,original_valid,not original_valid)
            if set(found)!=set(selected):
                raise ValueError(f'Missing polygon codes: {sorted(set(selected)-set(found))}')
            yield found,actual


def metric_projection(lon,lat):
    """Local azimuthal-equidistant metric CRS centered at source origin."""
    crs=CRS.from_proj4(f'+proj=aeqd +lat_0={lat:.10f} +lon_0={lon:.10f} +datum=WGS84 +units=m +no_defs')
    return Transformer.from_crs(CRS.from_epsg(4326),crs,always_xy=True)


def road_overlay(poly,origin,coords,u,v,code,labels,components, *, chunk=250_000):
    """Exact intersection with selected ROAD edges; metrics in local AEQD meters.

    A line whose endpoints are both outside the polygon remains eligible if it
    crosses it. Chunked envelope testing avoids a countrywide STRtree or O(E)
    Shapely object allocation. Only ROAD edges (graph code=0) are considered;
    ferry/gangway edges are excluded from these road-access summaries.
    """
    if len(u)!=len(v) or len(u)!=len(code):raise ValueError('Graph edge arrays disagree')
    if len(labels)!=len(coords):raise ValueError('Graph labels and coordinates disagree')
    if coords.shape[1]!=2:raise ValueError('Invalid graph geometry')
    minx,miny,maxx,maxy=poly.bounds
    fn=metric_projection(float(origin[0]),float(origin[1]))
    center=transform(fn.transform,Point(float(origin[0]),float(origin[1])))
    lengths=defaultdict(float); edge_counts=Counter()
    near={int(c):(math.inf,None) for c in components}
    for lo in range(0,len(u),chunk):
        hi=min(lo+chunk,len(u))
        uu=u[lo:hi];vv=v[lo:hi]; cc=code[lo:hi]
        road=cc==0
        if not road.any():continue
        # Keep any segment intersecting the axis-aligned polygon envelope.
        a=coords[uu];b=coords[vv]
        candidate=road & (np.minimum(a[:,0],b[:,0])<=maxx) & (np.maximum(a[:,0],b[:,0])>=minx) & \
            (np.minimum(a[:,1],b[:,1])<=maxy) & (np.maximum(a[:,1],b[:,1])>=miny)
        for j in np.flatnonzero(candidate):
            src=int(uu[j]);dst=int(vv[j])
            comp=int(labels[src]);
            if int(labels[dst])!=comp:
                raise ValueError(f'Original graph edge links different computed components: {src}, {dst}')
            line=LineString((tuple(a[j]),tuple(b[j])))
            if not poly.intersects(line):continue
            inter=line.intersection(poly)
            if inter.is_empty:continue
            piece=transform(fn.transform,inter)
            if not piece.length>0:continue  # point touch is not a segment inside
            lengths[comp]+=piece.length
            edge_counts[comp]+=1
            if comp in near:
                distance=center.distance(piece)
                if distance < near[comp][0]:
                    near[comp]=(float(distance),(src,dst))
    return {
        'component_lengths_m':dict(lengths),
        'component_edge_counts':dict(edge_counts),
        'component_nearest_segment_m':{c:(v[0] if math.isfinite(v[0]) else None) for c,v in near.items()},
        'nearest_segment_edge_nodes':{c:v[1] for c,v in near.items()},
    }


def classify(has_current,has_main,origin_inside,current_inside,alternative_inside):
    """Descriptive GIS observation, not a judgement about legal access."""
    if not origin_inside:return 'ORIGIN_OUTSIDE_SOURCE_POLYGON_REVIEW'
    if has_current and has_main:return 'DIFFERENT_GRAPH_COMPONENTS_HAVE_ROAD_SEGMENTS_INSIDE_POLYGON'
    if has_current and not has_main:return 'CURRENT_COMPONENT_ROADS_INSIDE; MAIN_NETWORK_NOT_INSIDE'
    if has_main and not has_current:return 'MAIN_COMPONENT_ROADS_INSIDE; CURRENT_COMPONENT_NOT_INSIDE'
    return 'NEITHER_TESTED_COMPONENT_HAS_ROAD_SEGMENTS_INSIDE_POLYGON'


def _v51_access(workspace,routing_hash,rank):
    root=workspace/'output'/'topology_osm_investigation'/routing_hash[:16]
    meta=root/'investigation_summary.json'
    if not meta.is_file():return None
    info=ga._read_json(meta)
    if info.get('routing_hash')!=routing_hash or info.get('status')!='LOCAL_OSM_ID_EVIDENCE_ONLY':
        return None
    rowsfile=root/'nearby_osm_ways.csv'
    if not rowsfile.is_file():return None
    items=[x for x in read_csv(rowsfile) if x.get('component','').startswith(f'rank{rank:02d}_')]
    seen={}
    for x in items:
        ident=(x.get('component'),x.get('osm_way_id'))
        seen[ident]=x
    restricted=sum(x.get('scenario') in ('restricted_or_area','excluded_access') for x in seen.values())
    private=sum(x.get('access','').lower()=='private' for x in seen.values())
    return {'restricted':restricted,'private':private,'windows':len({i[0] for i in seen})}


def execute(args):
    from .workspace import timed_step
    from .boundary_levels import ASSETS
    from .boundaries import RELEASE

    workspace=Path(args.workspace or (Path.home()/'Documents'/'net-distance-ph')).expanduser().resolve()
    run,manifest,_,routing_hash=ga._resolve_run(workspace,args.run_dir)
    root=workspace/'output'/'origin_snap_sensitivity'/routing_hash[:16]
    report=ga._read_json(root/'origin_snap_sensitivity.json')
    if report.get('schema')!=sa.SCHEMA or report.get('routing_hash')!=routing_hash or \
       report.get('all_modeled_original_component_mass_verified') is not True:
        raise ValueError('v52 origin-snap audit missing/stale; rerun audit-origin-snap')
    rows=choose_six(read_csv(root/'affected_barangay_origins.csv'))
    graph_path=workspace/'work'/'graph_full.npz'
    graph_sha=ga._sha256(graph_path)
    if graph_sha!=report.get('stage1_graph_sha256'):
        raise ValueError('Original graph differs from v52 source')
    core_key=manifest['core_cache_key']
    core=workspace/'variants'/'population_weighted'/'cores'/core_key[:16]
    core_meta=ga._read_json(core/'metadata.json')
    if core_meta.get('cache_key')!=core_key or core_meta.get('national_graph_sha256')!=graph_sha:
        raise ValueError('Saved routing core differs from v52 graph')
    if core_meta.get('points_file_sha256')!=report.get('geometry_file_sha256'):
        raise ValueError('Source geometry digest differs from the original run')
    repo=Path(__file__).resolve().parents[1]
    geom=sa.resolve_geometry(repo,workspace,core_meta)
    if str(geom.resolve())!=str(Path(report['geometry_source']).resolve()):
        raise ValueError('v52 geometry path no longer resolves to the saved source')
    geom_metadata=ga._read_json(geom.with_suffix('.json'))
    archive_name,expected_sha=ASSETS['barangay']
    source_record=geom_metadata.get('sources',[])
    if geom_metadata.get('geometry_source_policy')!='faeldon' or not any(
            s.get('source')=='faeldon/philippines-psgc-shapefiles' and s.get('sha256')==expected_sha
            for s in source_record):
        raise ValueError('v53 requires the cached, pinned faeldon ADM4 polygons used to construct these v52 origin coordinates')
    archive=workspace/'data'/'sources'/'psgc-aligned'/RELEASE/archive_name
    with timed_step('read six pinned, SHA-256-verified barangay polygons'):
        with pinned_barangay_polygons(archive,expected_sha) as pair:
            polygons,polygon_sha=pair
    with timed_step('reconstruct original graph connectivity and intersect road edges with six polygons'):
        with np.load(graph_path,allow_pickle=False) as gr:
            coords=gr['node_lonlat'];u=gr['u'];v=gr['v'];codes=gr['code']
        _,labels=labels_for_graph(len(coords),u,v)
        observations=[];geo_features=[]
        main_component=int(report['dominant_original_component'])
        for row in rows:
            bgy=row['barangay_psgc'];poly,valid,repaired=polygons[bgy]
            origin=(float(row['origin_lon']),float(row['origin_lat']))
            cur=int(row['current_node']);alt=int(row['nearest_main_road_node'])
            current_comp=int(row['original_component'])
            if min(cur,alt)<0 or max(cur,alt)>=len(coords):
                raise ValueError(f'Invalid original graph node in {bgy}')
            if int(labels[cur])!=current_comp or int(labels[alt])!=main_component:
                raise ValueError(f'Graph labels changed since v52 for {bgy}')
            if not np.allclose(coords[cur],(float(row['current_node_lon']),float(row['current_node_lat'])),atol=1e-8,rtol=0) or \
               not np.allclose(coords[alt],(float(row['nearest_main_road_lon']),float(row['nearest_main_road_lat'])),atol=1e-8,rtol=0):
                raise ValueError(f'Node coordinates disagree with saved v52 report: {bgy}')
            input_origin=Point(origin)
            snap_point=Point(coords[cur]);main_point=Point(coords[alt])
            inside_origin=bool(poly.covers(input_origin))
            inside_current=bool(poly.covers(snap_point))
            inside_main=bool(poly.covers(main_point))
            over=road_overlay(poly,origin,coords,u,v,codes,labels,(current_comp,main_component))
            lengths=over['component_lengths_m']; counts=over['component_edge_counts']
            nearest=over['component_nearest_segment_m']
            rank=int(row['component_rank'])
            nearby=_v51_access(workspace,routing_hash,rank)
            observation=classify(counts.get(current_comp,0)>0,counts.get(main_component,0)>0,
                                 inside_origin,inside_current,inside_main)
            obj={
               'barangay_psgc':bgy,'town_name':row['town_name'],'barangay_name':row['barangay_name'],
               'population':int(row['population']),'priority_rank':rank,
               'source_polygon_valid':valid,'polygon_repaired_for_measurement':repaired,
               'origin_inside_polygon':inside_origin,'current_snap_inside_polygon':inside_current,
               'main_alternative_inside_polygon':inside_main,
               'current_snap_m':round(float(row['current_snap_km'])*1000,2),
               'main_alternative_snap_m':round(float(row['nearest_main_road_km'])*1000,2),
               'additional_snap_m':round(float(row['additional_snap_km'])*1000,2),
               'current_component':current_comp,'main_component':main_component,
               'eligible_road_components_intersecting_polygon':len(counts),
               'main_component_has_roads_in_polygon':bool(counts.get(main_component,0)),
               'current_component_has_roads_in_polygon':bool(counts.get(current_comp,0)),
               'main_component_road_edges_intersecting_polygon':counts.get(main_component,0),
               'current_component_road_edges_intersecting_polygon':counts.get(current_comp,0),
               'main_component_road_length_inside_m':round(lengths.get(main_component,0.),2),
               'current_component_road_length_inside_m':round(lengths.get(current_comp,0.),2),
               'nearest_current_component_segment_inside_polygon_m':round(nearest[current_comp],2) if nearest[current_comp] is not None else '',
               'nearest_main_component_segment_inside_polygon_m':round(nearest[main_component],2) if nearest[main_component] is not None else '',
               'v51_gap_window_access_restricted_ways':nearby['restricted'] if nearby else '',
               'v51_gap_window_private_ways':nearby['private'] if nearby else '',
               'gis_observation':observation,
            }
            observations.append(obj)
            geo_features.append({'type':'Feature','geometry':mapping(poly),'properties':{'barangay_psgc':bgy,'kind':'original_polygon','original_valid':valid,'repaired_for_measurement':repaired}})
            for name,p in [('origin',origin),('saved_snap',tuple(coords[cur])),('alternative_main_snap',tuple(coords[alt]))]:
                geo_features.append({'type':'Feature','geometry':{'type':'Point','coordinates':list(map(float,p))},
                     'properties':{'barangay_psgc':bgy,'kind':name,'component':current_comp if name=='saved_snap' else main_component if name=='alternative_main_snap' else None}})
            for group,comp in [('current',current_comp),('main',main_component)]:
                edge=over['nearest_segment_edge_nodes'].get(comp)
                if edge is not None:
                    geo_features.append({'type':'Feature','geometry':{'type':'LineString','coordinates':[coords[edge[0]].tolist(),coords[edge[1]].tolist()]},
                         'properties':{'barangay_psgc':bgy,'kind':'nearest_in_polygon_road_segment_'+group,'component':comp}})
            print(f"POLYGON {bgy} {row['barangay_name']}: source/current/main inside={inside_origin}/{inside_current}/{inside_main}; "
                  f"road components={len(counts)}; current/main lengths={lengths.get(current_comp,0):.0f}/{lengths.get(main_component,0):.0f} m",flush=True)
    out=workspace/'output'/'polygon_network_consistency'/routing_hash[:16]
    out.mkdir(parents=True,exist_ok=True)
    ga._write_csv(out/'six_barangay_findings.csv',observations,list(CSV_COLS))
    geojson={'type':'FeatureCollection','features':geo_features}
    ga._write_json(out/'six_barangay_overlay.geojson',geojson)
    summary={
       'schema':SCHEMA,'date_utc':datetime.now(timezone.utc).isoformat(),
       'status':'READ_ONLY_GIS_POLYGON_NETWORK_VALIDATION',
       'routing_hash':routing_hash,'original_graph_sha256':graph_sha,
       'population_geometry_source':str(geom),'source_adm4_zip':str(archive),
       'source_adm4_zip_sha256':polygon_sha,
       'cases':[dict(x) for x in observations],
       'total_residents_in_six':sum(x['population'] for x in observations),
       'limitations':[
          'Intersection of a road with a polygon does not establish legal or physical access.',
          'Source polygon representative points are not measured residential population centers.',
          'All road edges have already passed the selected transport-release road/access filter. v51 gap-window access tags are context, not per-origin or per-polygon ground truth.',
          'Road lengths inside polygons use a local azimuthal-equidistant projection; they are descriptive, not routing distance.',
          'Polyline crossing, bridge/tunnel grade, flood barriers, and pedestrian access are not certified.',
          'No road links, population coordinates, snaps, graph edges, or routing outputs were changed.',
       ],
       'files':{'findings':'six_barangay_findings.csv','overlay':'six_barangay_overlay.geojson'},
    }
    ga._write_json(out/'polygon_network_consistency.json',summary)
    print('V53: six polygons inspected; no graph or origin changes',flush=True)
    print(f'V53 REPORT: {out / "polygon_network_consistency.json"}',flush=True)
    return 0
