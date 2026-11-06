"""
Turn road and ferry polylines into a noded, weighted, undirected graph, and
attach the municipal centroids to it.

Noding. The graph identifies vertices by rounded coordinates (six decimal
places, about 0.11 m). This heals numerical noise across shapefile layers,
but coordinate coincidence alone does not prove that two roads form a junction.
In particular, distinct OSM node IDs at a coincident XY position may represent
different grade-separated levels. Strict topological modeling would need to
preserve OSM node identities from extraction through graph construction.

The rounded coordinate is then used as *the* coordinate of the node, so an edge
between a given pair of nodes always gets exactly the same weight no matter
which way it is traversed or which polyline contributed it. That is a
precondition for the matrix coming out exactly symmetric.

Ferries. Road and ferry polylines go through the same noder, so ferry legs that
are chained end to end connect automatically, and a ferry terminal digitised on
top of a road vertex connects automatically too. Terminals digitised offshore
of the road end get an explicit *gangway* edge to the nearest road node within
``ferry_connect_km``, weighted by its own length so it cannot be used as a free
teleport.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from .geo import chord_to_arc_km, geodesic_km, lonlat_to_ecef

ROAD, FERRY, GANGWAY = 0, 1, 2


def node_coordinates(xy, snap_decimals=6):
    """Map raw vertices to node ids by rounded coordinate.

    Returns ``(node_id_per_vertex, node_lonlat)``.
    """
    scale = 10.0**snap_decimals
    q = np.rint(np.asarray(xy, dtype=np.float64) * scale).astype(np.int64)
    span = np.int64(round(360 * scale)) + 1
    key = (q[:, 0] + np.int64(round(180 * scale))) * span + (
        q[:, 1] + np.int64(round(90 * scale))
    )
    uniq, inv = np.unique(key, return_inverse=True)
    lon_q = uniq // span - np.int64(round(180 * scale))
    lat_q = uniq % span - np.int64(round(90 * scale))
    node_lonlat = np.column_stack([lon_q / scale, lat_q / scale])
    return inv.astype(np.int64), node_lonlat


def build_graph(layers, snap_decimals=6, geodesic_backend="auto", verbose=True):
    """Node a list of (Parts, layer_code) into (u, v, w, node_lonlat, layer_flags).

    ``layer_flags`` is an (n_nodes, 3) boolean array: column 0 marks nodes
    touched by a road, column 1 nodes touched by a ferry, and column 2 marks
    endpoints of ferry polyline parts (the only gangway candidates).
    """
    all_xy = np.concatenate([p.xy for p, _ in layers]) if layers else np.empty((0, 2))
    node_of_vertex, node_lonlat = node_coordinates(all_xy, snap_decimals)
    if verbose:
        print(f"  {all_xy.shape[0]:,} vertices -> {node_lonlat.shape[0]:,} nodes")

    us, vs, codes = [], [], []
    ferry_terminal_nodes = []
    base = 0
    for parts, code in layers:
        i, j, _ = parts.segment_endpoints()
        us.append(node_of_vertex[base + i])
        vs.append(node_of_vertex[base + j])
        codes.append(np.full(i.size, code, dtype=np.int8))

        # Only the first and last vertex of each ferry polyline part are
        # legitimate boarding/alighting candidates. Intermediate shape points
        # merely describe the curve of the route and must not become invented
        # ports just because they pass close to a road.
        if code == FERRY and parts.part_offset.size > 1:
            lo = parts.part_offset[:-1]
            hi = parts.part_offset[1:] - 1
            valid = hi > lo  # at least one actual segment
            if valid.any():
                terminal_vertex = np.concatenate([lo[valid], hi[valid]])
                ferry_terminal_nodes.append(node_of_vertex[base + terminal_vertex])

        base += parts.xy.shape[0]

    u = np.concatenate(us) if us else np.empty(0, np.int64)
    v = np.concatenate(vs) if vs else np.empty(0, np.int64)
    code = np.concatenate(codes) if codes else np.empty(0, np.int8)

    keep = u != v  # zero-length segments after rounding
    u, v, code = u[keep], v[keep], code[keep]

    w = geodesic_km(
        node_lonlat[u, 0], node_lonlat[u, 1],
        node_lonlat[v, 0], node_lonlat[v, 1],
        backend=geodesic_backend,
    )

    n = node_lonlat.shape[0]
    # Columns: road node, ferry node, ferry-polyline terminal. Keeping the
    # first two columns unchanged preserves compatibility with snap_sources().
    flags = np.zeros((n, 3), dtype=bool)
    for c, col in ((ROAD, 0), (FERRY, 1)):
        m = code == c
        flags[u[m], col] = True
        flags[v[m], col] = True
    if ferry_terminal_nodes:
        flags[np.concatenate(ferry_terminal_nodes), 2] = True

    if verbose:
        print(
            f"  {u.size:,} segments "
            f"({int((code == ROAD).sum()):,} road, {int((code == FERRY).sum()):,} ferry), "
            f"total length {w.sum():,.0f} km"
        )
    return u, v, w, code, node_lonlat, flags


def connect_ferries(
    u, v, w, code, node_lonlat, flags, ferry_connect_km=5.0, verbose=True,
    geodesic_backend="auto",
):
    """Connect offshore ferry *terminals* to the nearest road node.

    ``flags[:, 2]`` marks the endpoints of ferry polyline parts. Earlier
    versions connected every intermediate ferry shape point within range,
    which could invent boarding points along a coastline. A two-column flags
    array is still accepted for backward compatibility, but new graphs always
    provide the explicit terminal column.
    """
    is_road, is_ferry = flags[:, 0], flags[:, 1]
    is_terminal = flags[:, 2] if flags.shape[1] >= 3 else is_ferry
    orphan = np.flatnonzero(is_ferry & is_terminal & ~is_road)
    road_ids = np.flatnonzero(is_road)
    if orphan.size == 0 or road_ids.size == 0:
        if verbose:
            print("  no gangway edges needed")
        return u, v, w, code

    ecef = lonlat_to_ecef(node_lonlat[:, 0], node_lonlat[:, 1])
    tree = cKDTree(ecef[road_ids])
    chord, idx = tree.query(ecef[orphan], k=1)
    arc = chord_to_arc_km(chord)
    near = arc <= ferry_connect_km

    gu, gv = orphan[near], road_ids[idx[near]]
    gw = geodesic_km(
        node_lonlat[gu, 0], node_lonlat[gu, 1], node_lonlat[gv, 0], node_lonlat[gv, 1],
        backend=geodesic_backend,
    )
    if verbose:
        print(
            f"  {gu.size:,} terminal gangway edges added; "
            f"{int((~near).sum()):,} offshore ferry terminals had no road "
            f"within {ferry_connect_km} km"
        )
    return (
        np.concatenate([u, gu]),
        np.concatenate([v, gv]),
        np.concatenate([w, gw]),
        np.concatenate([code, np.full(gu.size, GANGWAY, np.int8)]),
    )


def apply_ferry_cost(w, code, multiplier=1.0, fixed_penalty_km=0.0):
    """Optionally turn plain distance into a generalised cost on ferry legs."""
    if multiplier == 1.0 and fixed_penalty_km == 0.0:
        return w
    w = w.copy()
    m = code == FERRY
    w[m] = w[m] * multiplier + fixed_penalty_km
    return w


def snap_sources(
    centroids_lonlat,
    node_lonlat,
    flags,
    max_snap_km=25.0,
    prefer_road=True,
    verbose=True,
    *,
    fallback_terminals_only=False,
    geodesic_backend="auto",
):
    """Attach each centroid to a graph node.

    Two-tier: prefer the nearest *road* node, since that is where a town's
    street network actually is; fall back to the nearest node of any kind;
    flag as unsnappable beyond ``max_snap_km`` rather than attaching to
    something implausible and silently poisoning a row of the matrix.

    With ``prefer_road`` the fallback can only find ferry nodes, and the
    nearest one is not always a terminal: it can be an intermediate shape
    point of a route that passes the island without stopping. With
    ``fallback_terminals_only=True`` such shape points (ferry-only nodes that
    are not polyline endpoints, ``flags[:, 2]``) are excluded from the
    fallback. The default keeps the historical behaviour.

    Returns ``(node_index, offset_km, tier)`` where tier is 0 road, 1 any,
    -1 unsnappable.
    """
    ecef_nodes = lonlat_to_ecef(node_lonlat[:, 0], node_lonlat[:, 1])
    ecef_cent = lonlat_to_ecef(centroids_lonlat[:, 0], centroids_lonlat[:, 1])
    n = centroids_lonlat.shape[0]

    node_idx = np.full(n, -1, dtype=np.int64)
    offset = np.full(n, np.inf)
    tier = np.full(n, -1, dtype=np.int8)

    def _query(mask, tier_code):
        ids = np.flatnonzero(mask)
        if ids.size == 0:
            return
        todo = np.flatnonzero(tier == -1)
        if todo.size == 0:
            return
        chord, k = cKDTree(ecef_nodes[ids]).query(ecef_cent[todo], k=1)
        arc = chord_to_arc_km(chord)
        ok = arc <= max_snap_km
        sel = todo[ok]
        node_idx[sel] = ids[k[ok]]
        # Report the true geodesic offset, not the chord-derived approximation.
        offset[sel] = geodesic_km(
            centroids_lonlat[sel, 0], centroids_lonlat[sel, 1],
            node_lonlat[node_idx[sel], 0], node_lonlat[node_idx[sel], 1],
            backend=geodesic_backend,
        )
        tier[sel] = tier_code

    if prefer_road:
        _query(flags[:, 0], 0)
    fallback = np.ones(node_lonlat.shape[0], dtype=bool)
    if fallback_terminals_only:
        if flags.shape[1] < 3:
            raise ValueError("fallback_terminals_only needs the ferry-terminal column flags[:, 2]")
        fallback &= ~(flags[:, 1] & ~flags[:, 0] & ~flags[:, 2])
    _query(fallback, 1)

    if verbose:
        print(
            f"  snapped {int((tier == 0).sum()):,} to road nodes, "
            f"{int((tier == 1).sum()):,} to ferry/other nodes, "
            f"{int((tier == -1).sum()):,} unsnappable (> {max_snap_km} km)"
        )
        fin = np.isfinite(offset)
        if fin.any():
            print(
                f"  snap offset: median {np.median(offset[fin]):.3f} km, "
                f"p95 {np.percentile(offset[fin], 95):.3f} km, "
                f"max {offset[fin].max():.3f} km"
            )
    return node_idx, offset, tier
