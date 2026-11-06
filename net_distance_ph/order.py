"""
Euclidean adjacency: source ordering and neighbour-priority wave radii.

Two jobs, both driven by the straight-line (geodesic) distances between the
municipal centroids. That matrix is 1,642 x 1,642 and costs milliseconds, so
it is worth computing up front purely to organise the expensive network work.

**Ordering.** Sources are processed in an order obtained by breadth-first
search over the Euclidean k-nearest-neighbour graph. Two consequences:

* Partial output is spatially coherent. Interrupt halfway and you have a
  complete sub-matrix for a contiguous region, not a scatter of rows.
* Batches sent to one Dijkstra call are geographically tight, which matters
  because scipy takes a single distance ``limit`` per call: the batch has to
  use the largest radius any of its members needs, and clustering keeps that
  from being dominated by one far-flung outlier.

**Waves.** The point of the neighbour priority. Rather than computing every row
to infinity in one go, run cheap radius-limited passes first: wave t restricts
Dijkstra to a radius derived from each town's t-th order neighbourhood in the
Euclidean kNN graph, and the last wave lifts the limit. A radius-limited
Dijkstra settles only the nodes inside the radius, so early waves are almost
free -- measured at ~2% of an unlimited run for a radius covering the first
neighbour ring -- and they fill in the near-neighbour block of the matrix
within seconds.

One thing to be precise about, because it is easy to assume more than is true.
Network distance is always >= straight-line distance, so a radius R derived
from Euclidean geometry guarantees this and only this:

    after a wave at radius R, every pair whose *network* distance is <= R has
    its exact final value; every pair still showing infinity is known only to
    be further than R.

It does *not* guarantee that town i's Euclidean t-hop neighbours have been
resolved. A town 30 km away across a bay can be 200 km by road, and it will
stay unresolved until a later wave. That is why each wave reports how many
towns actually resolved per row: it is the number to look at when deciding
whether the wave schedule is aggressive enough.
"""

from __future__ import annotations

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .geo import geodesic_km, lonlat_to_ecef


def euclidean_matrix(lonlat, block=256):
    """Full (N, N) geodesic straight-line distance between centroids, in km."""
    lonlat = np.asarray(lonlat, dtype=np.float64)
    n = lonlat.shape[0]
    D = np.empty((n, n), dtype=np.float64)
    for s in range(0, n, block):
        e = min(s + block, n)
        m = e - s
        lon1 = np.repeat(lonlat[s:e, 0], n)
        lat1 = np.repeat(lonlat[s:e, 1], n)
        lon2 = np.tile(lonlat[:, 0], m)
        lat2 = np.tile(lonlat[:, 1], m)
        D[s:e] = geodesic_km(lon1, lat1, lon2, lat2).reshape(m, n)
    np.fill_diagonal(D, 0.0)
    return np.minimum(D, D.T)  # kill any asymmetry from the blocked evaluation


def knn_graph(E, k=8):
    """Symmetric k-nearest-neighbour adjacency (boolean CSR) from a distance matrix."""
    n = E.shape[0]
    k = min(k, n - 1)
    idx = np.argpartition(E, kth=k, axis=1)[:, : k + 1]
    rows = np.repeat(np.arange(n, dtype=np.int64), idx.shape[1])
    cols = idx.ravel()
    keep = rows != cols
    A = coo_matrix(
        (np.ones(int(keep.sum()), dtype=bool), (rows[keep], cols[keep])), shape=(n, n)
    ).tocsr()
    return ((A + A.T) > 0).tocsr()


def bfs_order(A, lonlat=None, verbose=True):
    """Processing order: BFS over the kNN graph, components largest-first.

    Seeded at the town closest to the geometric centre of its component, so the
    order spirals outward instead of starting from a corner.
    """
    n = A.shape[0]
    ncomp, label = connected_components(A, directed=False)
    order = []
    for lab in np.argsort(-np.bincount(label, minlength=ncomp)):
        members = np.flatnonzero(label == lab)
        if members.size == 0:
            continue
        if lonlat is not None and members.size > 1:
            P = lonlat_to_ecef(lonlat[members, 0], lonlat[members, 1])
            seed = members[np.argmin(np.linalg.norm(P - P.mean(0), axis=1))]
        else:
            seed = members[0]
        seen = np.zeros(n, dtype=bool)
        seen[seed] = True
        frontier = [seed]
        while frontier:
            order.extend(frontier)
            nxt = np.unique(
                np.concatenate([A.indices[A.indptr[i] : A.indptr[i + 1]] for i in frontier])
            )
            nxt = nxt[~seen[nxt]]
            seen[nxt] = True
            frontier = list(nxt)
    order = np.array(order, dtype=np.int64)
    assert order.size == n and np.unique(order).size == n
    if verbose and ncomp > 1:
        print(f"  ordering: kNN graph has {ncomp} components, largest first")
    return order


def wave_radii(E, A, orders=(1, 2), scale=1.0, verbose=True):
    """Per-source radius for each wave, from t-hop Euclidean neighbourhoods.

    Returns a list of (N,) float arrays, one per entry of ``orders``. The
    caller appends the unlimited final wave.
    """
    n = E.shape[0]
    Ai = A.astype(np.int32)
    reach = Ai.copy()
    out = []
    hop = 1
    for t in sorted(orders):
        while hop < t:
            # Boolean reachability by counting paths and thresholding; int32
            # avoids the overflow a narrower dtype would hit on dense rows.
            reach = ((reach @ Ai) + reach)
            reach.data[:] = 1
            reach.eliminate_zeros()
            hop += 1
        R = np.zeros(n, dtype=np.float64)
        for i in range(n):
            nb = reach.indices[reach.indptr[i] : reach.indptr[i + 1]]
            if nb.size:
                R[i] = E[i, nb].max()
        R *= scale
        out.append(R)
        if verbose:
            print(
                f"  wave (Euclidean order {t}, scale {scale:g}): "
                f"radius median {np.median(R):.1f} km, p95 {np.percentile(R, 95):.1f} km, "
                f"max {R.max():.1f} km"
            )
    return out


def plan(lonlat, k=8, orders=(1, 2), scale=1.0, verbose=True):
    """Convenience wrapper. Returns (order, radii_list, euclidean_matrix)."""
    E = euclidean_matrix(lonlat)
    A = knn_graph(E, k=k)
    order = bfs_order(A, lonlat, verbose=verbose)
    radii = wave_radii(E, A, orders=orders, scale=scale, verbose=verbose)
    return order, radii, E
