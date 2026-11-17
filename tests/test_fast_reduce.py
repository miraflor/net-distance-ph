"""Exactness tests for the optional Numba fast-reduction path.

Small graph tests compare all terminal-to-terminal shortest distances against
unreduced SciPy, not merely the structural shape of the output.
"""
import numpy as np
import pytest
from scipy.sparse.csgraph import dijkstra

from net_distance_ph.fast_reduce import reduce_fast
from net_distance_ph.matrix import build_csr
from net_distance_ph.reduce import compact, dedupe_edges


def _assert_preserved(u, v, w, n, sources, *, checkpoint_alive=None):
    u, v, w = dedupe_edges(np.asarray(u, np.int64), np.asarray(v, np.int64),
                           np.asarray(w, np.float64), n)
    src = np.asarray(sources, np.int64)
    truth = dijkstra(build_csr(u, v, w, n), directed=False, indices=src)[:, src]
    ru, rv, rw, alive = reduce_fast(u, v, w, n, src, alive=checkpoint_alive)
    assert alive[src].all()
    assert not np.any(ru == rv)
    cu, cv, cw, cn, csrc = compact(ru, rv, rw, alive, src)
    got = dijkstra(build_csr(cu, cv, cw, cn), directed=False, indices=csrc)[:, csrc]
    assert np.array_equal(np.isfinite(truth), np.isfinite(got))
    mask = np.isfinite(truth)
    np.testing.assert_allclose(got[mask], truth[mask], rtol=1e-12, atol=1e-12)
    return len(cu), cn


def test_dead_end_and_road_chain():
    # One long chain connects 0->12, leaf spur 7->17 is irrelevant.
    u = list(range(12)) + [7, 13, 14, 15, 16]
    v = list(range(1, 13)) + [13, 14, 15, 16, 17]
    w = np.arange(1, len(u) + 1, dtype=float) / 7
    ne, nn = _assert_preserved(u, v, w, 18, [0, 12])
    assert ne == 1 and nn == 2


def test_source_in_spur_and_source_in_degree_two_chain():
    u = [0,1,2,3,4, 2,6,7,8, 4,10]
    v = [1,2,3,4,5, 6,7,8,9,10,11]
    w = [1.,2.,3.,4.,5., 1.,1.,1.,1., 1.,1.]
    ne, nn = _assert_preserved(u,v,w,12,[0,3,5,7,11])
    assert nn == 7  # branching junctions are intentionally preserved


def test_cyclic_paths_and_duplicate_shortcuts():
    # Square cycle with opposite terminal corners: two parallel paths.
    u = [0,1,2,3,4,5,6,7]
    v = [1,2,3,0,5,6,7,4]
    w = [1.,3.,4.,5.,2.,2.,2.,2.]
    ne, nn = _assert_preserved(u,v,w,9,[0,2,4,8])
    assert ne == 1 and nn == 4  # 0--2 only; source 4 isolated within separate cycle


def test_degree_reclassification_after_leaf_pruning():
    # Node 1 initially degree 3; pruning dangling 1--4 makes it degree 2.
    u = [0,1,2,1,4]
    v = [1,2,3,4,5]
    w = [1.,2.,3.,4.,5.]
    ne, nn = _assert_preserved(u,v,w,6,[0,3])
    assert ne == 1 and nn == 2


def test_two_disconnected_components_and_isolated_source():
    u = [0,1,2, 4,5,6, 8,9]
    v = [1,2,3, 5,6,7, 9,10]
    w = [1.,2.,3., 3.,4.,5., 2.,2.]
    ne, nn = _assert_preserved(u,v,w,12,[0,3,4,7,11])
    assert ne == 2 and nn == 5


def test_random_sparse_weighted_networks():
    rng = np.random.default_rng(2026)
    for size in (50, 400, 3000):
        pairs = set()
        # tree backbone + variable numbers of shortcuts; ensures sparse, mixed degrees
        for i in range(1,size):
            pairs.add((rng.integers(0,i),i))
        for _ in range(size//2):
            a,b = sorted(rng.choice(size,size=2,replace=False))
            pairs.add((a,b))
        uv=np.asarray(sorted(pairs))
        weights=rng.uniform(0.001,10.0,len(uv))
        _assert_preserved(uv[:,0],uv[:,1],weights,size,rng.choice(size,10,replace=False))


def test_old_checkpoint_is_accepted():
    # Simulate an already-contracted chain that bypasses inactive node 2.
    u=np.asarray([0,1,1],np.int64)
    v=np.asarray([1,3,4],np.int64)
    w=np.asarray([2.,3.,7.])
    mask=np.asarray([True,True,False,True,True])
    ne,nn=_assert_preserved(u,v,w,5,[0,3],checkpoint_alive=mask)
    assert ne==1 and nn==2


def test_bad_inputs_rejected():
    with pytest.raises(ValueError,match="nonnegative"):
        reduce_fast([0],[1],[-1.],2,[0,1])
    with pytest.raises(ValueError,match="self-loops"):
        reduce_fast([0],[0],[1.],2,[0])
    with pytest.raises(ValueError,match="checkpoint mask"):
        reduce_fast([0],[1],[1.],3,[0,1],alive=[True,False,False])
