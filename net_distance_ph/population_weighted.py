"""Optional population-weighted network representative distances.

Data interface is two plain provider-independent CSV tables, not PSA workbooks.
Default tables are generated once at installation and then read as CSV only.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

import numpy as np

from .population_data import code10, load_plain_dataset, export_bundled_defaults, sha256

SCHEMA = "net-distance-ph/population-weighted/v4"


def digest(path):
    return sha256(path)


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_points(path:Path):
    """Read 42k PSGC-tagged points; or validated polygons as representatives."""
    path=Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    points = {}
    if path.suffix.lower()==".csv":
        with path.open(newline="", encoding="utf-8-sig") as fd:
            table=csv.DictReader(fd)
            required={"psgc_code","lon","lat"}
            if not required.issubset(set(table.fieldnames or [])):
                raise ValueError(f"Point CSV requires columns {sorted(required)}")
            for r in table:
                code=code10(r["psgc_code"])
                if code in points:raise ValueError(f"Repeated barangay PSGC {code}")
                points[code]=(float(r["lon"]),float(r["lat"]))
    else:
        try:import geopandas as gpd
        except ImportError as exc:raise ImportError('GeoPackage/shapefile requires geopandas; install pip install -e ".[geo]"') from exc
        g=gpd.read_file(path)
        level_field=next((col for col in g.columns if col.casefold()=="geo_level"),None)
        if level_field is not None and g[level_field].astype(str).str.strip().eq("Bgy").any():
            g=g.loc[g[level_field].astype(str).str.strip().eq("Bgy")].copy()
        if g.crs is None:raise ValueError("Barangay geometry CRS is missing; refuse to infer EPSG")
        candidates={"psgc_code","adm4_psgc"}
        id_col=next((col for col in g.columns if col.casefold() in candidates),None)
        if id_col is None:raise ValueError("Barangay geometry requires psgc_code or adm4_psgc")
        if g.geometry.isna().any() or g.geometry.is_empty.any():
            raise ValueError("Barangay geometry contains missing/empty features")
        gg=g.to_crs("EPSG:4326")
        is_poly=gg.geom_type.isin(["Polygon","MultiPolygon"]).all()
        is_point=gg.geom_type.isin(["Point"]).all()
        if not (is_poly or is_point):raise ValueError("All barangay geometries must be points, or all polygons")
        loc=gg.geometry.representative_point() if is_poly else gg.geometry
        for code,x,y in zip(gg[id_col], loc.x, loc.y):
            k=code10(code)
            if k in points:raise ValueError(f"Repeated PSGC geometry {k}")
            points[k]=(float(x),float(y))
    for k,(x,y) in points.items():
        if not (np.isfinite(x) and np.isfinite(y) and 114<=x<=130 and 1<=y<=24):
            raise ValueError(f"Invalid Philippine longitude/latitude for {k}: {(x,y)}")
    return points



def validate_join(rows, points):
    required={r["psgc_code"] for r in rows}
    missing=sorted(required-points.keys())
    if missing:
        raise ValueError(f"{len(missing)} population-bearing barangays lack points, first {missing[:10]}")
    return np.array([points[r["psgc_code"]] for r in rows], dtype=np.float64)


def filter_origin_rows(rows, points, *, on_missing="exclude"):
    """Return routable rows, aligned coordinates, and missing-origin records.

    Missing geometry is distinct from disconnected network routes. Population
    stays in the *full* input roster for coverage denominators. A completely
    empty result is an explicit error: no empirical model can be constructed.
    """
    if on_missing not in ("exclude", "error"):
        raise ValueError("Missing geometry policy must be 'exclude' or 'error'")
    included, missing, positions = [], [], []
    for record in rows:
        code = record["psgc_code"]
        if code not in points:
            missing.append(record)
        else:
            included.append(record)
            positions.append(points[code])
    if missing and on_missing == "error":
        raise ValueError(f"{len(missing)} positive-population barangays lack geometry, "
                         f"first {[r['psgc_code'] for r in missing[:10]]}")
    if not included:
        raise ValueError("No positive-population barangays have usable geometry; "
                         "cannot construct a distance matrix")
    return included, np.asarray(positions, dtype=np.float64), missing


def select_sites(D, pop, offset, *, max_mean_access_km=None, max_sites=12):
    """Weighted network p-median: exact k=1; greedy + 1-swap k>1.

    D includes inf for disconnected demand nodes. Each component necessarily
    gets a representative. The access budget is explicit; no hidden k choice.
    """
    D=np.asarray(D, dtype=float)
    p=np.asarray(pop, dtype=float)
    off=np.asarray(offset,dtype=float)
    n=p.size
    if D.shape != (n,n) or not np.isfinite(p).all() or (p<=0).any() or not np.isfinite(off).all():
        raise ValueError("Invalid weighted-demand arrays")
    if max_sites<1:raise ValueError("max_sites must be positive")
    if max_mean_access_km is not None and (not np.isfinite(max_mean_access_km) or max_mean_access_km<0):
        raise ValueError("max_mean_access_km must be nonnegative and finite")
    if not np.isfinite(np.diag(D)).all():raise ValueError("Self-distances must be finite")
    # Find connectivity by finite distance. Identical node IDs belong together.
    remaining=set(range(n)); comps=[]
    while remaining:
        first=min(remaining)
        ids=np.flatnonzero(np.isfinite(D[first]))
        if not set(ids).issubset(remaining):
            # A component cannot intersect a previously extracted one.
            raise ValueError("Distance matrix has inconsistent connectivity")
        remaining.difference_update(map(int,ids))
        comps.append(ids)
    chosen=[]
    for ids in comps:
        sub=D[np.ix_(ids,ids)]
        # Sum of weighted distances per candidate (column).
        obj=p[ids] @ sub
        chosen.append(int(ids[int(np.argmin(obj))]))
    if len(chosen)>max_sites:
        raise ValueError(f"{len(chosen)} disconnected demand components require more than --max-sites-per-town={max_sites}")
    def assigned(choice):
        ds=D[:,choice]
        idx=np.argmin(ds,axis=1)
        best=ds[np.arange(n),idx]
        if not np.isfinite(best).all():raise ValueError("Some demand nodes have no reachable representative")
        return idx,best,float(np.dot(p,best+off)/p.sum())
    def swap_opt(choice):
        # Deterministic first-improvement 1-swap, bounded to prevent pathological search.
        for _ in range(3):
            idx,best,_=assigned(choice)
            base=float(p @ best)
            winning=None
            for pos in range(len(choice)):
                others=choice[:pos]+choice[pos+1:]
                baseline=np.min(D[:,others],axis=1) if others else np.full(n,np.inf)
                costs=np.sum(p[:,None]*np.minimum(baseline[:,None],D),axis=0)
                for candidate in choice:costs[candidate]=np.inf
                arg=int(np.argmin(costs))
                if np.isfinite(costs[arg]) and costs[arg] < base - 1e-10*max(base,1):
                    if winning is None or costs[arg]<winning[0]:winning=(float(costs[arg]),pos,arg)
            if winning is None:break
            choice=choice.copy();choice[winning[1]]=winning[2]
        return sorted(choice)
    chosen=swap_opt(chosen)
    while max_mean_access_km is not None and assigned(chosen)[2] > max_mean_access_km+1e-10:
        if len(chosen)>=max_sites:
            raise ValueError(f"Access error budget {max_mean_access_km:g} km unmet with {max_sites} sites; raise --max-sites-per-town or relax --max-mean-access-km")
        idx,best,_=assigned(chosen)
        new_cost=np.sum(p[:,None]*np.minimum(best[:,None],D),axis=0)
        new_cost[chosen]=np.inf
        candidate=int(np.argmin(new_cost))
        if not np.isfinite(new_cost[candidate]):raise ValueError("Cannot add a new representative")
        chosen=swap_opt(sorted(chosen+[candidate]))
    idx,best,error=assigned(chosen)
    weights=np.bincount(idx,weights=p,minlength=len(chosen))
    access=np.bincount(idx,weights=p*(best+off),minlength=len(chosen))/weights
    return np.asarray(chosen,dtype=np.int64), weights, access, error



def _component_curve(D, population, cap):
    """Deterministic approximate p-median cost curve for a CONNECTED component.

    The one-median is exact among candidate demand nodes; for k>1, greedy
    augmentation and deterministic 1-swaps search the discrete p-median space.
    Every returned cost is a population-weighted sum of *network* distances,
    excluding constant origin-to-road snap offsets.
    """
    n = len(population)
    if D.shape != (n, n) or not np.isfinite(D).all():
        raise ValueError("A component distance matrix must be square and finite")
    candidates = np.arange(n, dtype=int)
    selected = [int(np.argmin(np.asarray(population, float) @ D))]
    p = np.asarray(population, float)

    def current_cost(locations):
        return float(p @ np.min(D[:, locations], axis=1))

    def swap_improve(locations):
        # The objective is evaluated exactly for each proposed replacement.
        # This does not imply a globally optimal k-median for k>1.
        locations = list(sorted(locations))
        for _ in range(3):
            base_cost = current_cost(locations)
            best_choice = None
            for pos in range(len(locations)):
                remaining = locations[:pos] + locations[pos + 1:]
                base = (np.min(D[:, remaining], axis=1)
                        if remaining else np.full(n, np.inf))
                trial_costs = np.sum(p[:, None] * np.minimum(base[:, None], D), axis=0)
                trial_costs[locations] = np.inf
                trial = int(np.argmin(trial_costs))
                cost = float(trial_costs[trial])
                if cost < base_cost - 1e-10 * max(1., abs(base_cost)):
                    if best_choice is None or (cost, pos, trial) < best_choice:
                        best_choice = (cost, pos, trial)
            if best_choice is None:
                break
            _, pos, trial = best_choice
            locations[pos] = trial
            locations.sort()
        return locations

    curve = []
    for k in range(1, min(int(cap), n) + 1):
        if k > 1:
            old = np.min(D[:, selected], axis=1)
            scores = np.sum(p[:, None] * np.minimum(old[:, None], D), axis=0)
            scores[selected] = np.inf
            nxt = int(np.argmin(scores))
            if not np.isfinite(scores[nxt]):
                break
            selected = swap_improve(selected + [nxt])
        cost = current_cost(selected)
        curve.append((tuple(selected), cost))
        # No benefit is possible beyond exactly zero road access error.
        if cost <= 1e-10 * max(1., curve[0][1]):
            break
    return curve


def select_sites_balanced(D, pop, offset, *, max_sites=12):
    """Choose per-component medoids by a normalized rate-distortion objective.

    Let J(k_c) be the *heuristically minimized* weighted road access sum in
    component c. Among the enumerated choices with total k <= max_sites,
    minimize

        sum_c J(k_c)/sum_c J(1)
        + (sum_c k_c - n_components)/(n_demand - n_components).

    Both ratios run from 0 to 1; the second measures how much of the full
    demand support is retained beyond the mandatory one-per-component sites.
    This has no manually tuned distance threshold or penalty coefficient,
    but ``max_sites`` still matters. Each extra site adds 1/(n_demand -
    n_components) to the score, so the chosen k grows with the number of demand
    nodes. Synthetic examples sometimes hit the cap, but hitting it does
    NOT establish that the unconstrained score would prefer a larger k.
    ``reached_site_cap`` only records cap contact, not a binding-constraint test.
    It is a specified representation tradeoff, NOT a statistical guarantee of
    optimal k or a global solution to each NP-hard k-median problem.
    """
    D = np.asarray(D, dtype=float)
    p = np.asarray(pop, dtype=float)
    off = np.asarray(offset, dtype=float)
    n = len(p)
    if not n or D.shape != (n, n) or off.shape != (n,):
        raise ValueError('Invalid distance/population/offset shapes')
    if (not np.isfinite(p).all() or (p <= 0).any()
            or not np.isfinite(off).all() or (off < 0).any()):
        raise ValueError('Population and origin offsets must be finite and nonnegative')
    if np.isnan(D).any() or np.isneginf(D).any():
        raise ValueError('Road distance matrix contains NaN or -infinity')
    if not np.isfinite(np.diag(D)).all() or (D[np.isfinite(D)] < 0).any():
        raise ValueError('Road distances must be nonnegative with finite diagonals')
    if not np.array_equal(np.isfinite(D), np.isfinite(D.T)):
        raise ValueError('Asymmetric road reachability')
    if max_sites < 1:
        raise ValueError('max_sites must be positive')

    remaining = set(range(n))
    components = []
    while remaining:
        root = min(remaining)
        ids = np.flatnonzero(np.isfinite(D[root]))
        if not set(ids).issubset(remaining):
            raise ValueError('Inconsistent disconnected components')
        if not np.isfinite(D[np.ix_(ids, ids)]).all():
            raise ValueError('Non-transitive component reachability')
        remaining.difference_update(map(int, ids))
        components.append(ids)
    if len(components) > max_sites:
        raise ValueError(f'{len(components)} disconnected demand components require '
                         f'more than --max-sites-per-town={max_sites}')

    curves = []
    for ids in components:
        local = _component_curve(D[np.ix_(ids, ids)], p[ids], max_sites)
        curves.append([(tuple(int(ids[i]) for i in choice), cost)
                       for choice, cost in local])
    base = float(sum(curve[0][1] for curve in curves))
    n_extra = n - len(components)

    # Dynamic programming: for each total site count, use the minimum
    # *un-normalized* weighted access cost across all component allocations.
    # This also handles the global (per-town) max_sites cap correctly.
    frontier = {0: (0., ())}
    for curve in curves:
        next_frontier = {}
        for prev_k, (prev_cost, prev_choice) in frontier.items():
            for chosen, cost in curve:
                n_sites = prev_k + len(chosen)
                if n_sites > max_sites:
                    continue
                trial = (prev_cost + cost, prev_choice + chosen)
                old = next_frontier.get(n_sites)
                if (old is None or trial[0] < old[0] - 1e-10 * max(1., old[0])
                        or (abs(trial[0] - old[0]) <= 1e-10 * max(1., old[0])
                            and trial[1] < old[1])):
                    next_frontier[n_sites] = trial
        frontier = next_frontier
        if not frontier:
            raise ValueError('No feasible representative allocation within max_sites')

    def score(k, cost):
        loss = cost / base if base > 0 else 0.
        complexity = ((k - len(components)) / n_extra) if n_extra else 0.
        return (loss + complexity, k)

    best_k = min(frontier, key=lambda k: score(k, frontier[k][0]))
    road_cost, chosen = frontier[best_k]
    chosen = np.asarray(sorted(chosen), dtype=np.int64)
    # Keep the same population-assignment semantics as the previous model.
    all_dist = D[:, chosen]
    nearest = np.argmin(all_dist, axis=1)
    access_dist = all_dist[np.arange(n), nearest]
    if not np.isfinite(access_dist).all():
        raise ValueError('A population demand node was left without a reachable median')
    weights = np.bincount(nearest, weights=p, minlength=best_k)
    means = np.bincount(nearest, weights=p * (access_dist + off),
                        minlength=best_k) / weights
    total_mean = float(p @ (access_dist + off) / p.sum())
    score_value, _ = score(best_k, road_cost)
    diagnostics = {
        'criterion': 'balanced-normalized-rate-distortion/v1',
        'n_components': len(components),
        'n_demand_nodes': n,
        'n_sites': best_k,
        'road_access_baseline_km_per_person': float(base / p.sum()),
        'road_access_selected_km_per_person': float(road_cost / p.sum()),
        'normalized_road_access': float(road_cost / base) if base > 0 else 0.,
        'normalized_extra_sites': float((best_k - len(components)) / n_extra) if n_extra else 0.,
        'selection_score': float(score_value),
        'component_site_counts': [int(np.isin(chosen,c).sum()) for c in components],
        'evaluated_site_counts': [
            {'k': int(k),
             'road_access_relative': float(cost / base) if base > 0 else 0.,
             'complexity_fraction': float((k - len(components)) / n_extra) if n_extra else 0.,
             'score': float(score(k,cost)[0])}
            for k, (cost,_) in sorted(frontier.items())],
    }
    return chosen, weights, means, total_mean, diagnostics

def combine_group_matrices(town_num,town_den, mapping, n_groups):
    """Aggregate *unnormalized* population-pair totals (never mean of means)."""
    from scipy.sparse import csr_matrix
    mapping=np.asarray(mapping,dtype=int)
    good=np.flatnonzero(mapping>=0)
    M=csr_matrix((np.ones(good.size),(mapping[good],good)),shape=(n_groups,len(mapping)))
    a=(M@town_num)@M.T
    b=(M@town_den)@M.T
    return np.asarray(a),np.asarray(b)


def finish_means(numerator,denominator,pop_by_unit):
    n=np.asarray(numerator,dtype=np.float64)
    r=np.asarray(denominator,dtype=np.float64)
    if n.shape!=r.shape or n.shape[0]!=n.shape[1]:raise ValueError("Invalid pair-mass matrices")
    if not np.isfinite(n).all() or not np.isfinite(r).all() or (r<0).any():
        raise ValueError("Invalid weighted-distance totals")
    n=(n+n.T)/2
    r=(r+r.T)/2
    p=np.asarray(pop_by_unit,dtype=float)
    potential=np.outer(p,p)
    coverage=np.divide(r,potential,out=np.zeros_like(r),where=potential>0)
    if (coverage>1+1e-8).any() or (coverage< -1e-8).any():raise ValueError("Invalid reachability mass > population-pair mass")
    coverage=np.clip(coverage,0,1)
    conditional=np.divide(n,r,out=np.full_like(n,np.inf),where=r>0)
    # These are intra-unit expected distances, not zero. Preserve them.
    return conditional,coverage


def save_csv_matrix(path:Path, values, units):
    tmp=path.with_suffix(".tmp")
    with tmp.open("w",newline="",encoding="utf-8") as fd:
        wr=csv.writer(fd);wr.writerow(["psgc_code",*[u["code"] for u in units]])
        for unit,row in zip(units,values):
            wr.writerow([unit["code"],*["" if not np.isfinite(v) else f"{v:.6f}" for v in row]])
    os.replace(tmp,path)



def add_run_arguments(p):
    p.add_argument("--workspace", type=Path, default=Path.home() / "Documents" / "net-distance-ph", help="existing graph workspace")
    p.add_argument("--dataset-dir", type=Path, help="folder with units.csv and barangay_population.csv (default: bundled normalized tables)")
    p.add_argument("--points", type=Path, help="optional barangay points.csv with psgc_code,lon,lat; otherwise auto-discover/download pinned GIS geometry")
    p.add_argument("--level", choices=("town", "province", "province-equivalent", "region", "all"), default="all",
                   help="all three (default); town; province/province-equivalent (province + nonprovincial peers); region")
    p.add_argument("--median-selection", choices=("balanced", "one"), default="balanced",
                   help="balanced: automatic component-wise k via normalized access/complexity tradeoff (default); one: legacy 1-median per component")
    p.add_argument("--max-mean-access-km", type=float, default=None,
                   help="optional explicit mean-access budget (km); overrides balanced selection and uses legacy budgeted k-median")
    p.add_argument("--max-sites-per-town", type=int, default=12)
    p.add_argument("--max-snap-km", type=float, default=25.0)
    p.add_argument("--geometry-source", choices=("latest","auto","ocha","faeldon"), default="latest",
                   help="latest (default): reuse SHA-verified geometry of last run, else OCHA; explicit faeldon/ocha/auto override")
    p.add_argument("--unsnappable-policy",choices=("exclude","error"),default="exclude",
                   help="audit and exclude origins beyond snap radius, or fail strictly")
    p.add_argument("--missing-geometry-policy",choices=("exclude","error"),default="exclude",
                   help="audit and skip unmatched barangay geometry (default), or require complete matching")
    p.add_argument("--verify-sites", type=int, default=5)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--heartbeat-seconds", type=float, default=30.0,
                   help="seconds between elapsed-time liveness messages (default: 30)")
    p.add_argument("--allow-missing-population", action="store_true",
                   help="explicitly allow partial population coverage; missing barangays are excluded")
    p.add_argument("--province-crosswalk", type=Path,
                   help="optional town_psgc,province_psgc CSV for explicit nonprovincial-city assignments")
    p.add_argument("--no-auto-geometry", action="store_true",
                   help="fail if no matching barangay points are cached; no geometry download")


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    add_run_arguments(p)
    return p.parse_args(argv)


# Logging instrumentation
def run_options(args):
    from .population_routing import run, _PW_HEARTBEAT_SECONDS
    from .workspace import run_log, timed_step
    import math

    # Logging is deliberately outside the main compute path; even validation
    # failures are captured in a uniquely timestamped run log.
    with run_log(args.workspace):
        heartbeat = float(getattr(args, "heartbeat_seconds", 30.0))
        if not math.isfinite(heartbeat) or heartbeat <= 0:
            raise ValueError("--heartbeat-seconds must be finite and positive")
        token = _PW_HEARTBEAT_SECONDS.set(heartbeat)
        try:
            with timed_step("population-weighted (total)", heartbeat):
                return run(args)
        finally:
            _PW_HEARTBEAT_SECONDS.reset(token)


def main(argv=None):
    return run_options(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
