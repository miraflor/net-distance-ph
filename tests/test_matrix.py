import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest
from scipy.sparse.csgraph import dijkstra

import ph_net_distance.matrix as matrix_module
from ph_net_distance.matrix import (
    Checkpoint,
    build_csr,
    compute,
    drop_unreachable,
    expand_and_finalise,
    rcm_reorder,
    save_core,
)
from ph_net_distance.order import bfs_order, euclidean_matrix, knn_graph, wave_radii
from ph_net_distance.reduce import dedupe_edges


def _plain_E(pts):
    """Straight-line distances in the *same units as the edge weights*.

    ph_net_distance.order.euclidean_matrix returns geodesic km, which is right for real
    lon/lat but meaningless for a unit-square toy graph -- the radii would come
    out ~100x the graph diameter and every wave would resolve everything.
    """
    d = pts[:, None, :] - pts[None, :, :]
    return np.sqrt((d * d).sum(-1))


def _graph(n=1500, radius=0.05, seed=0):
    from scipy.spatial import cKDTree

    rng = np.random.default_rng(seed)
    pts = rng.random((n, 2))
    pairs = cKDTree(pts).query_pairs(radius, output_type="ndarray")
    u, v = pairs[:, 0], pairs[:, 1]
    w = np.linalg.norm(pts[u] - pts[v], axis=1)
    u, v, w = dedupe_edges(u, v, w, n)
    return u, v, w, n, pts


def _prepare(d, u, v, w, n, srcs):
    save_core(build_csr(u, v, w, n), d)
    return srcs


def test_waves_give_the_same_final_matrix():
    u, v, w, n, pts = _graph()
    srcs = np.random.default_rng(1).choice(n, 60, replace=False)
    G = build_csr(u, v, w, n)
    ref = dijkstra(G, directed=False, indices=srcs)[:, srcs]

    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        _prepare(d1, u, v, w, n, srcs)
        plain, r1 = compute(d1, srcs, waves=(), n_workers=2, batch_size=7, verbose=False)
        _prepare(d2, u, v, w, n, srcs)
        E = _plain_E(pts[srcs])
        A = knn_graph(E, k=6)
        waves = wave_radii(E, A, orders=(1, 2), scale=1.0, verbose=False)
        waved, r2 = compute(
            d2, srcs, order=bfs_order(A, verbose=False), waves=waves,
            n_workers=2, batch_size=7, verbose=False,
        )
    fin = np.isfinite(ref)
    assert np.array_equal(fin, np.isfinite(plain)) and np.array_equal(fin, np.isfinite(waved))
    assert np.max(np.abs(plain[fin] - ref[fin])) == 0.0
    assert np.max(np.abs(waved[fin] - ref[fin])) == 0.0
    assert np.isinf(r1).all() and np.isinf(r2).all()
    print("  waved result == unwaved result == scipy, exactly")


def test_partial_after_a_wave_is_sound():
    """The invariant the whole scheme rests on.

    After a wave, every finite entry must be the exact final distance, and
    every infinite entry must correspond to a pair genuinely further apart than
    that row's validity radius. Nothing may be finite-but-wrong, and nothing
    within the radius may be missing.
    """
    u, v, w, n, pts = _graph(seed=4)
    srcs = np.random.default_rng(5).choice(n, 50, replace=False)
    ref = dijkstra(build_csr(u, v, w, n), directed=False, indices=srcs)[:, srcs]

    captured = {}

    def grab(wi, label, D, radius):
        if wi == 0:
            captured["D"] = np.array(D)
            captured["r"] = radius.copy()

    with tempfile.TemporaryDirectory() as d:
        _prepare(d, u, v, w, n, srcs)
        E = _plain_E(pts[srcs])
        A = knn_graph(E, k=5)
        compute(
            d, srcs, order=bfs_order(A, verbose=False),
            waves=wave_radii(E, A, orders=(1,), scale=1.0, verbose=False),
            n_workers=2, batch_size=6, on_wave_end=grab, verbose=False,
        )

    D, r = captured["D"], captured["r"]
    got = np.isfinite(D)
    assert np.all(D[got] == ref[got]), "a resolved entry disagrees with the truth"
    missing = ~got & np.isfinite(ref)
    if missing.any():
        rows = np.nonzero(missing)[0]
        assert np.all(ref[missing] > r[rows] - 1e-12), "a pair inside the radius was missed"
    frac = got.sum() / got.size
    print(f"  wave 1 resolved {frac:.0%} of pairs; every one exact, none inside radius missed")
    assert 0.0 < frac < 1.0, "the test radius should resolve some but not all pairs"


def test_resume_mid_wave():
    u, v, w, n, pts = _graph(seed=2)
    srcs = np.random.default_rng(3).choice(n, 30, replace=False)
    with tempfile.TemporaryDirectory() as d:
        _prepare(d, u, v, w, n, srcs)
        full = np.array(compute(d, srcs, n_workers=1, batch_size=5, verbose=False)[0])

        # Simulate a crash: half the rows lose their radius and their contents.
        r = np.load(Path(d) / "core_radius.npy")
        r[15:] = 0.0
        np.save(Path(d) / "core_radius.npy", r)
        D = np.lib.format.open_memmap(Path(d) / "core_matrix.npy", mode="r+")
        D[15:] = np.inf
        D.flush()
        del D

        resumed, rr = compute(d, srcs, n_workers=2, batch_size=4, verbose=False)
    assert np.array_equal(np.isfinite(full), np.isfinite(resumed))
    fin = np.isfinite(full)
    assert np.max(np.abs(full[fin] - resumed[fin])) == 0.0
    assert np.isinf(rr).all()
    print("  resume reproduces the uninterrupted result bit for bit")


def test_checkpoint_from_another_core_is_not_resumed():
    """Same source count, different graph: the old rows must be discarded.

    Earlier versions checked only the shape, resumed the old rows, and kept
    them through np.minimum, so a longer new distance could never appear.
    """
    u, v, w, n, _ = _graph(seed=12)
    srcs = np.random.default_rng(13).choice(n, 30, replace=False)
    with tempfile.TemporaryDirectory() as d:
        _prepare(d, u, v, w, n, srcs)
        compute(d, srcs, n_workers=1, batch_size=5, verbose=False)
        w2 = w * 1.7                                    # every distance changes
        _prepare(d, u, v, w2, n, srcs)
        got, radius = compute(d, srcs, n_workers=1, batch_size=5, verbose=False)
    ref = dijkstra(build_csr(u, v, w2, n), directed=False, indices=srcs)[:, srcs]
    fin = np.isfinite(ref)
    assert np.array_equal(fin, np.isfinite(got))
    assert np.max(np.abs(got[fin] - ref[fin])) == 0.0
    assert np.isinf(radius).all()


def test_checkpoint_without_fingerprint_is_not_resumed(tmp_path):
    """A checkpoint written by an older version carries no fingerprint."""
    ck = Checkpoint(tmp_path, 3)                       # fingerprint=None: legacy layout
    ck.D[:] = 1.0
    ck.radius[:] = np.inf
    ck.flush()
    ck.close()
    ck = Checkpoint(tmp_path, 3, fingerprint="abc")
    assert np.isinf(np.asarray(ck.D)).all() and (ck.radius == 0).all()
    ck.close()


class _StopAfterFirstRows(RuntimeError):
    pass


_ORIGINAL_WORK = matrix_module._work


def _slow_work(task):
    time.sleep(0.4)
    return _ORIGINAL_WORK(task)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="timing-sensitive: spawn start-up of workers is slow on Windows and macOS runners")
def test_failure_stops_workers_without_draining_the_queue(monkeypatch):
    """On an exception (Ctrl-C included) the pool is terminated, not drained.

    Earlier versions called close() + join(), which waits for every queued
    Dijkstra batch, so an interrupted national run appeared to hang.
    """
    u, v, w, n, _ = _graph(seed=14)
    srcs = np.random.default_rng(15).choice(n, 80, replace=False)
    monkeypatch.setattr(matrix_module, "_work", _slow_work)
    absorbed = []

    def absorb_then_stop(self, batch, rows, limit):
        absorbed.append(batch)
        if len(absorbed) == 2:
            raise _StopAfterFirstRows

    monkeypatch.setattr(Checkpoint, "absorb", absorb_then_stop)
    with tempfile.TemporaryDirectory() as d:
        _prepare(d, u, v, w, n, srcs)
        t0 = time.monotonic()
        with pytest.raises(_StopAfterFirstRows):
            compute(d, srcs, n_workers=2, batch_size=2, verbose=False)
        elapsed = time.monotonic() - t0
    # 40 batches x 0.4 s / 2 workers = 8 s if the queue were drained.
    assert elapsed < 5.0, f"compute took {elapsed:.1f}s to stop"


def test_rcm_changes_no_distance():
    u, v, w, n, _ = _graph(seed=8)
    srcs = np.random.default_rng(9).choice(n, 25, replace=False)
    ref = dijkstra(build_csr(u, v, w, n), directed=False, indices=srcs)[:, srcs]
    ru, rv, rw, rs = rcm_reorder(u, v, w, n, srcs, verbose=False)
    got = dijkstra(build_csr(ru, rv, rw, n), directed=False, indices=rs)[:, rs]
    fin = np.isfinite(ref)
    assert np.array_equal(fin, np.isfinite(got))
    assert np.max(np.abs(ref[fin] - got[fin])) == 0.0
    print("  RCM relabelling is distance-preserving")


def test_source_order_does_not_change_the_answer():
    u, v, w, n, _ = _graph(seed=6)
    srcs = np.random.default_rng(7).choice(n, 40, replace=False)
    outs = []
    for seed in (0, 1):
        with tempfile.TemporaryDirectory() as d:
            _prepare(d, u, v, w, n, srcs)
            o = np.random.default_rng(seed).permutation(len(srcs))
            outs.append(np.array(compute(d, srcs, order=o, n_workers=2, batch_size=6, verbose=False)[0]))
    fin = np.isfinite(outs[0])
    assert np.array_equal(fin, np.isfinite(outs[1]))
    assert np.max(np.abs(outs[0][fin] - outs[1][fin])) == 0.0
    print("  processing order is irrelevant to the final values")


def test_drop_unreachable_preserves_distances():
    u1, v1, w1, n1, _ = _graph(700, 0.07, 11)
    u2, v2, w2, n2, _ = _graph(700, 0.07, 12)
    u = np.concatenate([u1, u2 + n1])
    v = np.concatenate([v1, v2 + n1])
    w = np.concatenate([w1, w2])
    n = n1 + n2
    srcs = np.array([5, 9, 40, 300])
    ref = dijkstra(build_csr(u, v, w, n), directed=False, indices=srcs)[:, srcs]
    du, dv, dw, dn, dsrc = drop_unreachable(u, v, w, n, srcs, verbose=False)
    got = dijkstra(build_csr(du, dv, dw, dn), directed=False, indices=dsrc)[:, dsrc]
    assert dn < n and np.max(np.abs(ref - got)) == 0.0
    print(f"  dropped {n - dn:,} source-free nodes with no change in distances")


def test_expand_offsets_radius_and_symmetry():
    D_core = np.array([[0.0, 4.0, np.inf], [4.0, 0.0, np.inf], [np.inf, np.inf, 0.0]])
    inv = np.array([0, 0, 1, 2, 0])
    offset = np.array([0.5, 0.25, 1.0, 2.0, 0.0])
    snapped = np.array([True, True, True, True, False])
    radius = np.array([np.inf, 10.0, np.inf])
    D, trad, rep = expand_and_finalise(D_core, radius, inv, offset, snapped, verbose=False)
    assert np.array_equal(D, D.T) and (np.diag(D) == 0).all()
    assert np.isclose(D[0, 1], 0.75), "co-located towns still separated by their stubs"
    assert np.isclose(D[0, 2], 4 + 0.5 + 1.0)
    assert np.isinf(D[3, 0]) and np.isinf(D[4, 0])
    assert trad[2] == 10.0 and np.isinf(trad[0]) and np.isinf(trad[4])
    assert rep["n_rows_complete"] == 4
    print("  expansion carries stubs, radii, co-location and unreachability")


def test_bfs_order_is_a_permutation_even_when_disconnected():
    rng = np.random.default_rng(0)
    ll = np.concatenate([
        np.column_stack([rng.normal(121, 0.4, 60), rng.normal(14, 0.4, 60)]),
        np.column_stack([rng.normal(125, 0.4, 40), rng.normal(7, 0.4, 40)]),
    ])
    A = knn_graph(euclidean_matrix(ll), k=3)
    o = bfs_order(A, ll, verbose=False)
    assert np.array_equal(np.sort(o), np.arange(len(ll)))
    print("  BFS order is a permutation across disconnected clusters")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("OK")
