import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

from net_distance_ph.reduce import compact, dedupe_edges, reduce_to_core


def _dedupe_ref(u, v, w):
    """Independent (slow, dict-based) parallel-edge min reduction.

    scipy's coo -> csr *sums* duplicate entries, so the reference graph has to
    be made simple before it is built, or the oracle itself would be wrong.
    Deliberately not reusing net_distance_ph.reduce.dedupe_edges, to keep this an
    independent check.
    """
    best = {}
    for a, b, x in zip(u, v, w):
        a, b = int(a), int(b)
        if a == b:
            continue
        k = (min(a, b), max(a, b))
        if k not in best or x < best[k]:
            best[k] = x
    if not best:
        return (np.empty(0, np.int64),) * 2 + (np.empty(0, float),)
    ks = sorted(best)
    return (
        np.array([k[0] for k in ks], np.int64),
        np.array([k[1] for k in ks], np.int64),
        np.array([best[k] for k in ks], float),
    )


def _csr(u, v, w, n):
    u, v, w = _dedupe_ref(u, v, w)
    return coo_matrix(
        (np.concatenate([w, w]), (np.concatenate([u, v]), np.concatenate([v, u]))),
        shape=(n, n),
    ).tocsr()


def _apsp(u, v, w, n, srcs):
    return dijkstra(_csr(u, v, w, n), directed=False, indices=srcs)[:, srcs]


def _check(u, v, w, n, srcs, max_degree=4, label=""):
    ref = _apsp(u, v, w, n, srcs)
    ru, rv, rw, alive = reduce_to_core(u, v, w, n, srcs, max_degree=max_degree, verbose=False)
    cu, cv, cw, cn, csrcs = compact(ru, rv, rw, alive, srcs)
    got = _apsp(cu, cv, cw, cn, csrcs)

    finite = np.isfinite(ref)
    assert np.array_equal(finite, np.isfinite(got)), f"{label}: reachability changed"
    err = np.max(np.abs(ref[finite] - got[finite]) / np.maximum(ref[finite], 1e-12)) if finite.any() else 0.0
    assert err < 1e-12, f"{label}: max rel error {err}"
    shrink = 100 * cn / max(int((np.bincount(np.concatenate([u, v]), minlength=n) > 0).sum()), 1)
    print(f"  {label:<34} nodes -> {shrink:5.1f}%  edges {len(u):>7,} -> {len(cu):>7,}  err {err:.1e}")
    return cn, len(cu)


def _geometric(n, radius, seed, dim=2):
    rng = np.random.default_rng(seed)
    pts = rng.random((n, dim))
    from scipy.spatial import cKDTree

    pairs = cKDTree(pts).query_pairs(radius, output_type="ndarray")
    u, v = pairs[:, 0], pairs[:, 1]
    w = np.linalg.norm(pts[u] - pts[v], axis=1)
    return u, v, w


def test_random_geometric():
    """The closest analogue to a road network: sparse, planar-ish, weighted."""
    for seed in range(6):
        n = 3000
        u, v, w = _geometric(n, 0.033, seed)
        rng = np.random.default_rng(100 + seed)
        srcs = rng.choice(n, 60, replace=False)
        _check(u, v, w, n, srcs, label=f"geometric seed={seed}")


def test_grid_with_chains():
    """Grids have many degree-2 and degree-3 nodes: the contraction's best case."""
    k = 40
    idx = lambda r, c: r * k + c
    u, v, w = [], [], []
    rng = np.random.default_rng(7)
    for r in range(k):
        for c in range(k):
            if c + 1 < k:
                u.append(idx(r, c)); v.append(idx(r, c + 1)); w.append(rng.uniform(0.5, 2))
            if r + 1 < k:
                u.append(idx(r, c)); v.append(idx(r + 1, c)); w.append(rng.uniform(0.5, 2))
    u, v, w = map(np.array, (u, v, w))
    srcs = rng.choice(k * k, 40, replace=False)
    _check(u, v, w, k * k, srcs, label="40x40 grid")


def test_long_chains_between_hubs():
    """Densified polylines: hubs joined by long chains of degree-2 shape points."""
    rng = np.random.default_rng(11)
    n_hub, chain = 30, 200
    u, v, w = [], [], []
    nid = n_hub
    for _ in range(60):
        a, b = rng.choice(n_hub, 2, replace=False)
        prev = a
        for _ in range(chain):
            u.append(prev); v.append(nid); w.append(rng.uniform(0.01, 0.05)); prev = nid; nid += 1
        u.append(prev); v.append(b); w.append(rng.uniform(0.01, 0.05))
    u, v, w = map(np.array, (u, v, w))
    cn, ce = _check(u, v, w, nid, np.arange(n_hub), label="hubs + degree-2 chains")
    assert cn <= n_hub + 5, "chains should collapse essentially completely"


def test_multi_edges_self_loops_and_islands():
    rng = np.random.default_rng(5)
    n = 1200
    u, v, w = _geometric(n, 0.05, 3)
    # Duplicate some edges with worse weights, and add self-loops.
    dup = rng.choice(len(u), 300)
    u = np.concatenate([u, u[dup], np.arange(50)])
    v = np.concatenate([v, v[dup], np.arange(50)])
    w = np.concatenate([w, w[dup] * 3, np.zeros(50)])
    srcs = rng.choice(n, 40, replace=False)
    _check(u, v, w, n, srcs, label="multi-edges + self-loops")


def test_disconnected_and_isolated_sources():
    """Islands with no ferry: infinities must stay infinite and stay symmetric."""
    u1, v1, w1 = _geometric(600, 0.06, 21)
    u2, v2, w2 = _geometric(600, 0.06, 22)
    u = np.concatenate([u1, u2 + 600])
    v = np.concatenate([v1, v2 + 600])
    w = np.concatenate([w1, w2])
    n = 1250  # nodes 1200..1249 are isolated sources
    srcs = np.concatenate([[3, 17, 400], [700, 900], np.arange(1200, 1210)])
    _check(u, v, w, n, srcs, label="two components + isolated")


def test_source_is_never_contracted():
    u, v, w = _geometric(800, 0.05, 31)
    srcs = np.arange(0, 800, 7)
    ru, rv, rw, alive = reduce_to_core(u, v, w, 800, srcs, verbose=False)
    assert alive[srcs].all(), "a source node was contracted away"
    print("  sources all survive contraction")


def test_dedupe_keeps_minimum():
    u = np.array([0, 1, 1, 0, 2])
    v = np.array([1, 0, 0, 1, 2])
    w = np.array([5.0, 2.0, 9.0, 3.0, 0.0])
    du, dv, dw = dedupe_edges(u, v, w, 3)
    assert list(du) == [0] and list(dv) == [1] and dw[0] == 2.0
    print("  dedupe keeps min weight and drops self-loops")


def test_max_degree_monotone_shrink():
    u, v, w = _geometric(4000, 0.03, 41)
    srcs = np.random.default_rng(0).choice(4000, 50, replace=False)
    sizes = {}
    for d in (2, 3, 4, 5):
        sizes[d] = _check(u, v, w, 4000, srcs, max_degree=d, label=f"max_degree={d}")
    assert sizes[4][0] <= sizes[2][0], "higher max_degree should not grow the core"


def test_reduction_can_resume_after_completed_pass():
    """A resumed reduction preserves all source-to-source shortest distances."""
    u, v, w = _geometric(1800, 0.043, 77)
    srcs = np.random.default_rng(78).choice(1800, 45, replace=False)
    ref = _apsp(u, v, w, 1800, srcs)

    saved = {}

    class StopAfterCheckpoint(RuntimeError):
        pass

    def checkpoint(state):
        saved.update(
            u=state["u"].copy(),
            v=state["v"].copy(),
            w=state["w"].copy(),
            alive=state["alive"].copy(),
            best_edges=state["best_edges"],
            start_phase=state["next_phase"],
            start_pass=state["next_pass"],
            completed_passes=state["completed_passes"],
            initial_nodes=state["initial_nodes"],
            initial_edges=state["initial_edges"],
        )
        raise StopAfterCheckpoint

    try:
        reduce_to_core(
            u, v, w, 1800, srcs,
            verbose=False,
            checkpoint=checkpoint,
            heartbeat_seconds=0,
        )
    except StopAfterCheckpoint:
        pass

    ru, rv, rw, alive = reduce_to_core(
        saved["u"], saved["v"], saved["w"], 1800, srcs,
        verbose=False,
        alive=saved["alive"],
        best_edges=saved["best_edges"],
        start_phase=saved["start_phase"],
        start_pass=saved["start_pass"],
        completed_passes=saved["completed_passes"],
        initial_nodes=saved["initial_nodes"],
        initial_edges=saved["initial_edges"],
        heartbeat_seconds=0,
    )
    cu, cv, cw, cn, csrcs = compact(ru, rv, rw, alive, srcs)
    got = _apsp(cu, cv, cw, cn, csrcs)

    finite = np.isfinite(ref)
    assert np.array_equal(finite, np.isfinite(got))
    assert np.max(np.abs(ref[finite] - got[finite])) < 1e-12


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("OK")
