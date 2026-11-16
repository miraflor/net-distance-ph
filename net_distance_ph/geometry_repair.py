"""Conservative, provenance-recorded repair of invalid source polygons.

Only a normalized *derived* geometry is repaired; the pinned upstream archive
is never modified. A repair cannot silently alter a routing centroid or area.
"""
from __future__ import annotations

import hashlib
import math

# A repair is accepted only if its relative absolute area change is <= 0.01%,
# its bounding box moves <= 1 m, and its centroid moves <= 100 m or 0.1% of
# the bounding-box diagonal (whichever is smaller, with a 1 m floor).
MAX_AREA_CHANGE_REL = 0.0001
MAX_BOUND_SHIFT_M = 1.0
MAX_CENTROID_SHIFT_M = 100.0


def _checksum(geom):
    return hashlib.sha256(geom.wkb).hexdigest()


def repair_polygons(frame, *, id_column: str, name_column: str):
    """Return (copy_of_frame, repair_records), refusing substantive changes.

    Coordinates must be in a projected CRS with metre units. The API never
    removes records, conflates PSGC identities, or modifies the source ZIP.
    """
    import shapely
    from shapely.validation import explain_validity

    if frame.crs is None or not frame.crs.is_projected:
        raise ValueError('Geometry repair requires a declared projected source CRS')
    units = {str(axis.unit_name).lower() for axis in frame.crs.axis_info[:2]}
    if not units or not all(u in {'metre', 'meter', 'metres', 'meters'} for u in units):
        raise ValueError(f'Geometry repair requires metre units, got {sorted(units)}')
    changed = frame.copy()
    repairs = []
    for idx in frame.index[~frame.geometry.is_valid]:
        original = frame.geometry.loc[idx]
        code = str(frame.at[idx, id_column])
        name = str(frame.at[idx, name_column])
        reason = explain_validity(original)
        orig_area = float(original.area)
        orig_centroid = original.centroid
        oxmin, oymin, oxmax, oymax = original.bounds
        diag = math.hypot(oxmax - oxmin, oymax - oymin)
        if not math.isfinite(orig_area) or orig_area <= 0:
            raise ValueError(f'UNSAFE geometry {idx} {code} {name}: {reason}; original area is zero/nonfinite; manual review required')
        if original.geom_type not in {'Polygon', 'MultiPolygon'}:
            raise ValueError(f'UNSAFE geometry {idx} {code} {name}: nonpolygon source')
        candidates = []
        for method in ('structure', 'linework'):
            fixed = shapely.make_valid(original, method=method, keep_collapsed=(method == 'linework'))
            # Reject GeometryCollections, lines or points: these cannot be
            # silently stripped while preserving an administrative boundary.
            if fixed.geom_type not in {'Polygon', 'MultiPolygon'} or fixed.is_empty or not fixed.is_valid:
                continue
            area_rel = abs(float(fixed.area) - orig_area) / orig_area
            bounds_shift = max(abs(float(a) - float(b)) for a, b in zip(original.bounds, fixed.bounds))
            centroid_shift = float(orig_centroid.distance(fixed.centroid))
            allowed_centroid = min(MAX_CENTROID_SHIFT_M, max(1.0, diag * 0.001))
            if not all(map(math.isfinite, (area_rel, bounds_shift, centroid_shift))):
                continue
            candidates.append((area_rel, centroid_shift, bounds_shift, method, fixed, allowed_centroid))
        valid = [x for x in candidates if x[0] <= MAX_AREA_CHANGE_REL
                 and x[1] <= x[5] and x[2] <= MAX_BOUND_SHIFT_M]
        if not valid:
            examples = [dict(method=x[3], relative_area_change=x[0], centroid_shift_m=x[1],
                             bounds_shift_m=x[2]) for x in candidates]
            raise ValueError(
                f'UNSAFE geometry {idx} {code} {name}: {reason}; '
                f'no conservative polygon repair within limits. Candidates={examples}. '
                'Manual inspection required; no boundary release published.'
            )
        area_rel, centroid_shift, bound_shift, method, fixed, _ = min(valid, key=lambda x: (x[0], x[1], x[2]))
        changed.at[idx, changed.geometry.name] = fixed
        record = dict(row=int(idx), source_code=code, source_name=name,
                      invalidity=reason, repair_method=f'shapely.make_valid({method})',
                      original_wkb_sha256=_checksum(original), repaired_wkb_sha256=_checksum(fixed),
                      original_type=original.geom_type, repaired_type=fixed.geom_type,
                      original_area_m2=orig_area, repaired_area_m2=float(fixed.area),
                      relative_area_change=area_rel, centroid_shift_m=centroid_shift,
                      bounds_shift_m=bound_shift)
        repairs.append(record)
        print(f'GEOMETRY REPAIR ACCEPTED: {code} {name} (source row {idx}): '
              f'{reason}; method={method}; area change={area_rel:.3g}; '
              f'centroid shift={centroid_shift:.3f} m', flush=True)
    if not changed.geometry.is_valid.all():
        raise AssertionError('Repair returned an invalid polygon')
    return changed, repairs
