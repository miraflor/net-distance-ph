"""Provider-neutral barangay point preparation with explicit OCHA-first provenance.

OCHA COD-AB geometries are older than the 2026 PSGC roster. We match by a
separate, flat, unique code-crosswalk (no fuzzy names and no silent rekeying).
faeldon's pinned level-4 geometry fills genuine OCHA coverage gaps. Both sources
provide geometry only: all populations and parents come from the plain dataset.
"""
from __future__ import annotations

from collections import Counter
import csv
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from zipfile import ZipFile

import numpy as np

from .population_data import code10, sha256, _write_csv

OCHA_API = 'https://data.humdata.org/api/3/action/package_show?id=cod-ab-phl'
OCHA_SOURCE = 'https://data.humdata.org/dataset/cod-ab-phl'


def _csv_cached(path, meta, *, require=None):
    if not path.is_file():
        return False
    if not meta.is_file():
        raise ValueError(f'Geometry cache exists without manifest: {path}')
    info=json.loads(meta.read_text(encoding='utf-8'))
    if info.get('points_sha256') != sha256(path):
        raise ValueError(f'Cached geometry checksum mismatch: {path}')
    source_audit=path.with_name(path.stem+'_source_audit.csv')
    if info.get('source_audit_sha256') and (not source_audit.is_file()
            or sha256(source_audit)!=info['source_audit_sha256']):
        raise ValueError(f'Cached geometry source audit has been changed or lost: {source_audit}')
    if require is not None and info.get('requested_code_sha256') != require:
        return False
    return True


def _dataset_codes(roster):
    return {k for k,v in roster.items() if v['level']=='barangay'}


def _code_digest(codes):
    import hashlib
    return hashlib.sha256(('\n'.join(sorted(codes))+'\n').encode('ascii')).hexdigest()


def _normalized_ocha_pcode(value):
    text=str(value or '').strip().upper().replace(' ','')
    if text.startswith('PH'): text=text[2:]
    if not re.fullmatch(r'\d{9,10}',text):return None
    return 'PH'+text


def _crosswalk(dataset_dir, required):
    """Read a *flat* optional correspondence table, independent of PSA formats.

    Old OCHA p-codes are nine digits with a PH prefix. A direct match is also
    allowed, but we never infer a code translation from geometry or names.
    """
    forward={c:{'PH'+c,'PH'+c.lstrip('0')} for c in required}
    path=Path(dataset_dir)/'geometry_crosswalk.csv'
    if not path.is_file():return forward, None
    with path.open(newline='',encoding='utf-8-sig') as fd:
        reader=csv.DictReader(fd)
        if not {'psgc_code','ocha_pcode'}.issubset(reader.fieldnames or []):
            raise ValueError('geometry_crosswalk.csv requires psgc_code,ocha_pcode')
        used={}
        for line,record in enumerate(reader,2):
            k=code10(record['psgc_code'])
            if k not in required:continue
            old=_normalized_ocha_pcode(record['ocha_pcode'])
            if old is None:continue
            if old in used and used[old]!=k:
                # One old geometry cannot be assigned to two new barangays.
                # Both fall back to the explicitly current PSGC geometry.
                forward[used[old]].discard(old)
                forward[k].discard(old)
                used[old]=None
                continue
            if old in used and used[old] is None:continue
            forward[k].add(old)
            used[old]=k
    return forward,sha256(path)


def _read_adm4_from_archive(archive):
    """Extract *only* ADM4 sidecars, never unpack an entire national archive."""
    import geopandas as gpd
    with ZipFile(archive) as z:
        names=[n for n in z.namelist() if n.lower().endswith('.shp') and ('adm4' in Path(n).name.lower())]
        if len(names)!=1:
            raise ValueError(f'OCHA archive must contain exactly one ADM4 SHP; got {len(names)}')
        shp=names[0]
        stem=shp[:-4]
        related={Path(n).suffix.lower():n for n in z.namelist() if n.lower().startswith(stem.lower()+'.')}
        for ext in ('.shp','.shx','.dbf','.prj'):
            if ext not in related:raise ValueError(f'OCHA ADM4 missing {ext}')
        if sum(z.getinfo(n).file_size for n in related.values())>5_000_000_000:
            raise ValueError('OCHA ADM4 uncompressed data unusually large; require manual review')
        with tempfile.TemporaryDirectory(prefix='ndph-ocha-adm4-') as temporary:
            root=Path(temporary)
            for ext,n in related.items():
                with z.open(n) as src,(root/('adm4'+ext)).open('wb') as dst:
                    shutil.copyfileobj(src,dst,4*1024*1024)
            return gpd.read_file(root/'adm4.shp')


def _choose_ocha_resource():
    """Resolve the CURRENT official HDX package instead of inventing a URL."""
    req=Request(OCHA_API,headers={'User-Agent':'net-distance-ph/0.1 research; provenance-preserving'})
    with urlopen(req,timeout=20) as resp:
        payload=json.load(resp)
    if not payload.get('success'):raise ValueError('HDX API did not return a successful package')
    resources=payload.get('result',{}).get('resources',[])
    eligible=[]
    for row in resources:
        name=(' '.join(str(row.get(k) or '') for k in ('name','url','format'))).casefold()
        if 'shp' not in name or '.zip' not in name:continue
        url=str(row.get('url') or '')
        parts=urlparse(url)
        # Only acquire files via the authoritative HDX hostname. If HDX
        # changes to a signed CDN, explicitly review that new host first.
        if parts.scheme!='https' or parts.hostname not in {'data.humdata.org','proxy.hdx.rwlabs.org'}:
            continue
        eligible.append((1 if 'phl_admin_boundaries' in name else 0,url,row))
    if not eligible:raise ValueError('No officially hosted SHP ZIP resource found via HDX API')
    eligible.sort(key=lambda x:(x[0],str(x[2].get('last_modified') or '')),reverse=True)
    return eligible[0][1],eligible[0][2]


def _download_ocha(archive):
    """Checksum tracked but not claimed pinned; source URL and content hash archived."""
    info=archive.with_suffix('.json')
    if archive.is_file() and info.is_file():
        recorded=json.loads(info.read_text(encoding='utf-8'))
        if recorded.get('sha256')==sha256(archive) and recorded.get('source')=='OCHA COD-AB Philippines':
            print(f'REUSE: checksum-verified OCHA archive {archive}',flush=True)
            return recorded
    url,resource=_choose_ocha_resource()
    archive.parent.mkdir(parents=True,exist_ok=True)
    if archive.is_file():
        raise ValueError(f'OCHA download exists without matching provenance; do not overwrite: {archive}')
    tmp=archive.with_name(archive.name+'.part')
    if tmp.exists():raise FileExistsError(f'Incomplete OCHA download found: {tmp}')
    print(f'DOWNLOADING OCHA COD-AB: {url}',flush=True)
    try:
        req=Request(url,headers={'User-Agent':'net-distance-ph/0.1 research'})
        with urlopen(req,timeout=90) as source,tmp.open('xb') as out:
            length=int(source.headers.get('Content-Length') or 0)
            if length and length+1_500_000_000>shutil.disk_usage(archive.parent).free:
                raise OSError('Insufficient disk for OCHA archive plus temporary ADM4 extraction')
            shutil.copyfileobj(source,out,4*1024*1024)
        with ZipFile(tmp) as z:
            if z.testzip() is not None:raise ValueError('OCHA archive checksum failed')
        os.replace(tmp,archive)
        recorded={'source':'OCHA COD-AB Philippines','url':url,
                  'resource_id':resource.get('id'),'sha256':sha256(archive)}
        meta_tmp=info.with_name(info.name+'.tmp')
        meta_tmp.write_text(json.dumps(recorded,indent=2)+'\n',encoding='utf-8')
        os.replace(meta_tmp,info)
        return recorded
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _frame_points(frame, code_column, *, source):
    import geopandas as gpd
    if frame.crs is None:raise ValueError(f'{source} geometry has no CRS')
    if frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise ValueError(f'{source} has null/empty polygons')
    if not frame.geometry.geom_type.isin(('Polygon','MultiPolygon')).all():
        raise ValueError(f'{source} ADM4 contains nonpolygon geometry')
    p=frame.to_crs('EPSG:6933').geometry.representative_point().to_crs('EPSG:4326')
    for code,x,y in zip(frame[code_column].tolist(),p.x.to_numpy(),p.y.to_numpy()):
        if not(np.isfinite(x) and np.isfinite(y) and 114<=x<=130 and 1<=y<=24):
            raise ValueError(f'{source} geometry produced invalid coordinate for {code!r}')
        yield str(code),(float(x),float(y))


def _source_ocha(workspace,dataset_dir,needed):
    archive=workspace/'data'/'sources'/'ocha'/'cod-ab-phl'/'phl_admin_boundaries.shp.zip'
    metadata=_download_ocha(archive)
    frame=_read_adm4_from_archive(archive)
    ccol=next((c for c in frame.columns if c.lower()=='adm4_pcode'),None)
    if ccol is None:raise ValueError('OCHA ADM4 is missing ADM4_PCODE; no name matching permitted')
    forward,correspondence_sha=_crosswalk(dataset_dir,needed)
    inverse={}
    for new,old_codes in forward.items():
        for old in old_codes:inverse.setdefault(old,set()).add(new)
    matched={}
    for code,point in _frame_points(frame,ccol,source='OCHA'):
        norm=_normalized_ocha_pcode(code)
        dest=inverse.get(norm,()) if norm else ()
        if len(dest)==1:
            new=next(iter(dest))
            if new in matched:
                raise ValueError(f'OCHA repeated geometry matches {new}; refusing ambiguous coordinates')
            matched[new]=point
    print(f'OCHA: resolved {len(matched):,}/{len(needed):,} requested barangays; remainder needs PSGC-updated geometry',flush=True)
    return matched,{**metadata,'crosswalk_sha256':correspondence_sha,'matched':len(matched)}


def _source_faeldon(workspace,needed):
    from .boundary_levels import ASSETS
    from .boundaries import _download, _read_shapefile, RELEASE
    archive_name,expected_sha=ASSETS['barangay']
    archive=workspace/'data'/'sources'/'psgc-aligned'/RELEASE/archive_name
    url=f'https://github.com/faeldon/philippines-psgc-shapefiles/releases/download/{RELEASE}/{archive_name}'
    _download(archive,url=url,expected_sha=expected_sha)
    frame=_read_shapefile(archive)
    if 'geo_level' in frame.columns:
        frame=frame.loc[frame['geo_level'].astype(str).str.strip().eq('Bgy')].copy()
    ccol=next((c for c in frame.columns if c.lower()=='adm4_psgc'),None)
    if ccol is None:raise ValueError('faeldon geometry has no adm4_psgc')
    result={}
    for code,point in _frame_points(frame,ccol,source='faeldon'):
        pc=code10(code)
        if pc in needed:
            if pc in result:raise ValueError(f'Duplicate faeldon PSGC {pc}')
            result[pc]=point
    return result,{'source':'faeldon/philippines-psgc-shapefiles','sha256':expected_sha,'url':url,'matched':len(result)}


def _published_points(workspace):
    level=workspace/'levels'/'barangay'/'data'
    pointer=level/'current_boundaries.json'
    if not pointer.is_file():return None
    info=json.loads(pointer.read_text(encoding='utf-8'))
    rel=Path(info['centroids']);loc=(level/rel).resolve()
    if not loc.is_relative_to(level.resolve()) or not loc.is_file():
        raise ValueError('Invalid published barangay point selection path')
    return loc


def discover_points(workspace,dataset_dir,roster,*,requested=None,auto_geometry=True,source='ocha'):
    """Default OCHA-only; explicit auto uses OCHA then faeldon as a fallback."""
    workspace=Path(workspace).expanduser().resolve()
    dataset_dir=Path(dataset_dir).expanduser().resolve()
    if requested is not None:
        chosen=Path(requested).expanduser().resolve()
        if not chosen.is_file():raise FileNotFoundError(chosen)
        print(f'POINT SOURCE: explicitly supplied {chosen}',flush=True)
        return chosen
    explicit=dataset_dir/'barangay_points.csv'
    if explicit.is_file():
        print(f'POINT SOURCE: user-provided plain CSV {explicit}',flush=True)
        return explicit
    if source not in ('auto','ocha','faeldon'):
        raise ValueError('Expected geometry source auto, ocha, or faeldon')
    required=_dataset_codes(roster)
    base_key=_code_digest(required)
    import hashlib
    crosswalk_file=dataset_dir/'geometry_crosswalk.csv'
    crosswalk_hash=sha256(crosswalk_file) if crosswalk_file.is_file() else 'none'
    key=hashlib.sha256(f'{base_key}:{crosswalk_hash}'.encode('ascii')).hexdigest()
    out=workspace/'variants'/'population_weighted'/'geometry'
    output=out/f'barangay_points_{source}_{key[:12]}.csv'
    meta=output.with_suffix('.json')
    if _csv_cached(output,meta,require=key):
        print(f'REUSE: {source} geometry policy, pinned point CSV {output}',flush=True)
        return output
    if not auto_geometry:
        raise FileNotFoundError('No barangay coordinates cached for selected source; --no-auto-geometry prohibits downloads')
    resolved={};lineage=[];sources=[];warnings=[]
    if source!='faeldon':
        try:
            matched,info=_source_ocha(workspace,dataset_dir,required)
            resolved.update(matched)
            sources.append(info)
            for code in matched:lineage.append({'psgc_code':code,'geometry_source':'OCHA','geometry_status':'matched_via_2023_pcode'})
        except (OSError,ValueError,KeyError,ImportError) as exc:
            if source=='ocha':raise
            message=f'OCHA unavailable or incompatible ({type(exc).__name__}: {exc}); using faeldon backup'
            warnings.append(message)
            print(f'FALLBACK: {message}',flush=True)
    missing=required-set(resolved)
    if missing and source!='ocha':
        fallback,info=_source_faeldon(workspace,missing)
        sources.append(info)
        for code,coords in fallback.items():
            resolved[code]=coords
            lineage.append({'psgc_code':code,'geometry_source':'faeldon','geometry_status':'2026_psgc_fallback'})
    missing=required-set(resolved)
    if missing:
        warnings.append(f'{len(missing)} barangays have no geometry under {source} policy; '
                        'their population will be audited and excluded from routed origins')
        print(f'GEOMETRY COVERAGE WARNING: {len(missing):,}/{len(required):,} barangays '
              f'not matched under {source} policy; no synthetic origins', flush=True)
    if not resolved:
        raise ValueError(f'No barangay geometry was matched under source policy {source}; '
                         'cannot construct any representative origins')
    out.mkdir(parents=True,exist_ok=True)
    rows=[{'psgc_code':c,'lon':f'{xy[0]:.9f}','lat':f'{xy[1]:.9f}'} for c,xy in sorted(resolved.items())]
    _write_csv(output,('psgc_code','lon','lat'),rows)
    audit=output.with_name(output.stem+'_source_audit.csv')
    lineage.extend({'psgc_code':code,'geometry_source':'',
                    'geometry_status':'missing_geometry'} for code in sorted(missing))
    _write_csv(audit,('psgc_code','geometry_source','geometry_status'),sorted(lineage,key=lambda x:x['psgc_code']))
    record={'schema':'net-distance-ph/plain-geometry/v3','points_sha256':sha256(output),
            'requested_code_sha256':key,'n_points':len(rows),
            'n_unmatched':len(missing),'unmatched_codes':sorted(missing),
            'geometry_source_policy':source,
            'sources':sources,'warnings':warnings,'source_audit_csv':str(audit),
            'source_audit_sha256':sha256(audit),'interpretation':'points on admin polygons, not residential addresses'}
    tmp=meta.with_name(meta.name+'.tmp')
    tmp.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    os.replace(tmp,meta)
    print(f'POINTS READY: {len(rows):,}/{len(required):,} with coordinates; '
          f'{len(missing):,} missing, audited (source policy: {source})', flush=True)
    return output
