"""Near-linear terminal-preserving reduction for a weighted undirected graph.

This fast path removes source-free leaves and contracts maximal degree-2 chains.
Unlike degree-3/4 vertex elimination, it does not introduce dense shortcut
cliques. Degree >=3 junctions remain in the core. Both transformations preserve
all terminal-to-terminal shortest-path distances for nonnegative edge weights.

Designed for very large national road networks. Numba kernels operate on compact
NumPy arrays; no Python per-node loops, repeated adjacency sorting, or pass-by-
pass compression of multi-hundred-megabyte checkpoints. The final, typically
much smaller shortcut list is sorted once to deduplicate parallel edges.
"""
from __future__ import annotations

import time
from typing import Callable

import numpy as np

try:
    from numba import njit
except ImportError as exc:
    raise ImportError(
        "Fast reduction requires Numba, a required dependency of net-distance-ph. "
        "Reinstall the package or run: python -m pip install 'numba>=0.59'"
    ) from exc


@njit(cache=True)
def _make_incidence(u, v, n_nodes):
    """CSR containing edge IDs (not duplicated weights)."""
    degree = np.zeros(n_nodes, dtype=np.int32)
    m = u.size
    for e in range(m):
        degree[u[e]] += 1
        degree[v[e]] += 1
    ptr = np.empty(n_nodes + 1, dtype=np.int64)
    ptr[0] = 0
    for i in range(n_nodes):
        ptr[i + 1] = ptr[i] + degree[i]
    cursor = ptr[:-1].copy()
    adj = np.empty(2 * m, dtype=np.int32)
    for e in range(m):
        a, b = u[e], v[e]
        adj[cursor[a]] = e
        cursor[a] += 1
        adj[cursor[b]] = e
        cursor[b] += 1
    return ptr, adj, degree


@njit(cache=True)
def _peel_nonterminal_leaves(u, v, ptr, adj, degree, protected):
    """Remove all nonterminal degree-0/1 vertices in one queue traversal."""
    n = degree.size
    m = u.size
    active_edge = np.ones(m, dtype=np.bool_)
    pruned = np.zeros(n, dtype=np.bool_)
    queue = np.empty(n, dtype=np.int32)
    head = 0
    tail = 0
    for i in range(n):
        if degree[i] <= 1 and not protected[i]:
            queue[tail] = i
            tail += 1
    while head < tail:
        node = queue[head]
        head += 1
        if pruned[node] or protected[node] or degree[node] > 1:
            continue
        pruned[node] = True
        for pos in range(ptr[node], ptr[node + 1]):
            e = adj[pos]
            if not active_edge[e]:
                continue
            active_edge[e] = False
            other = v[e] if u[e] == node else u[e]
            degree[other] -= 1
            if degree[other] == 1 and not protected[other]:
                queue[tail] = other
                tail += 1
        degree[node] = 0
    return active_edge, pruned


@njit(cache=True)
def _walk_chains(u, v, w, ptr, adj, degree, active_edge, kept):
    """Return one exact shortcut per maximal path between retained vertices."""
    # Each shortcut needs two endpoint stubs; this is a tight safe bound.
    total_stubs = 0
    for node in range(degree.size):
        if kept[node]:
            total_stubs += degree[node]
    limit = total_stubs // 2
    new_u = np.empty(limit, dtype=np.int64)
    new_v = np.empty(limit, dtype=np.int64)
    new_w = np.empty(limit, dtype=np.float64)
    count = 0
    for start in range(degree.size):
        if not kept[start]:
            continue
        for pos in range(ptr[start], ptr[start + 1]):
            first_edge = adj[pos]
            if not active_edge[first_edge]:
                continue
            node = v[first_edge] if u[first_edge] == start else u[first_edge]
            preceding_edge = first_edge
            path_weight = w[first_edge]
            # Degree-2 vertices have exactly one remaining edge other than the
            # incoming one. Their paths are disjoint except at kept vertices.
            while not kept[node]:
                next_edge = -1
                for j in range(ptr[node], ptr[node + 1]):
                    e = adj[j]
                    if e != preceding_edge and active_edge[e]:
                        next_edge = e
                        break
                if next_edge == -1:
                    # By construction, every non-kept live node has degree 2.
                    # Defensive sentinel to avoid a runaway loop on corruption.
                    raise ValueError("inconsistent degree-2 path during fast reduction")
                path_weight += w[next_edge]
                preceding_edge = next_edge
                node = v[next_edge] if u[next_edge] == node else u[next_edge]
            if start < node:
                new_u[count] = start
                new_v[count] = node
                new_w[count] = path_weight
                count += 1
            # start == node: returning positive-weight cycle is unnecessary;
            # start > node: the reverse visit already emits this path.
    return new_u[:count], new_v[:count], new_w[:count]


def reduce_fast(
    u, v, w, n_nodes, source_nodes, *, alive=None,
    progress: Callable[[str], None] | None = None,
):
    """Fast exact core: peel terminal-free trees, replace degree-2 chains.

    Inputs must be a de-duplicated, loop-free, undirected edge list; the stage-2
    driver already enforces this. Existing legacy reduction checkpoints are
    valid inputs, because they have already replaced removed nodes with exact
    shortcuts. A checkpoint's ``alive`` mask is only validated (every source
    and edge endpoint must be alive); the retained set is derived again here.

    Returns (u, v, w, alive), compatible with ``reduce.compact``.
    """
    from net_distance_ph.reduce import dedupe_edges

    emit = progress or (lambda msg: None)
    t0 = time.monotonic()
    u = np.ascontiguousarray(u, dtype=np.int64)
    v = np.ascontiguousarray(v, dtype=np.int64)
    w = np.ascontiguousarray(w, dtype=np.float64)
    src = np.asarray(source_nodes, dtype=np.int64)
    n_nodes = int(n_nodes)
    if n_nodes < 0 or u.shape != v.shape or u.shape != w.shape:
        raise ValueError("invalid graph shapes for fast reduction")
    if np.any(u < 0) or np.any(v < 0) or np.any(u >= n_nodes) or np.any(v >= n_nodes):
        raise ValueError("edge endpoint outside graph")
    if np.any(u == v):
        raise ValueError("fast reducer requires no self-loops")
    if np.any(~np.isfinite(w)) or np.any(w < 0):
        raise ValueError("fast reducer requires finite nonnegative weights")
    if np.any(src < 0) or np.any(src >= n_nodes):
        raise ValueError("source endpoint outside graph")
    if n_nodes >= 2**31 or u.size >= 2**31:
        raise ValueError("fast reduction currently requires fewer than 2^31 nodes/edges")
    if alive is not None and np.asarray(alive).size:
        prev = np.asarray(alive, dtype=bool)
        if prev.shape != (n_nodes,):
            raise ValueError("checkpoint alive mask has invalid shape")
        if np.any(~prev[src]) or np.any(~prev[u]) or np.any(~prev[v]):
            raise ValueError("checkpoint mask omits a source or an edge endpoint")

    protected = np.zeros(n_nodes, dtype=np.bool_)
    protected[src] = True
    emit(f"  fast reduction: {n_nodes:,} nodes / {u.size:,} edges; protecting {src.size:,} origins")
    emit("  building adjacency (first use also compiles fast kernels)...")
    ptr, adj, degree = _make_incidence(u, v, n_nodes)
    emit(f"  adjacency ready in {time.monotonic() - t0:.1f}s")
    t1 = time.monotonic()
    active_edge, pruned = _peel_nonterminal_leaves(u, v, ptr, adj, degree, protected)
    emit(f"  leaf pruning: removed {int(pruned.sum()):,} nodes / "
         f"{u.size - int(active_edge.sum()):,} edges in {time.monotonic() - t1:.1f}s")

    kept = protected | ((degree > 0) & (degree != 2))
    # A connected component without terminals that is only a cycle of degree-2
    # nodes is never walked and therefore dropped. A terminal-free component
    # that contains a junction (degree >= 3) is kept; callers remove such
    # components first with matrix.drop_unreachable.
    t2 = time.monotonic()
    ru, rv, rw = _walk_chains(u, v, w, ptr, adj, degree, active_edge, kept)
    emit(f"  chain compression: {ru.size:,} candidate shortcuts in {time.monotonic() - t2:.1f}s")
    # Required: scipy COO -> CSR sums parallel edge weights; choose minimum.
    ru, rv, rw = dedupe_edges(ru, rv, rw, n_nodes)
    # Also preserve legacy checkpoint source nodes with no remaining edges.
    final_alive = kept
    emit(f"  fast core: {int(final_alive.sum()):,} nodes / {ru.size:,} edges; "
         f"total {time.monotonic() - t0:.1f}s (before RCM/output)")
    return ru, rv, rw, final_alive
