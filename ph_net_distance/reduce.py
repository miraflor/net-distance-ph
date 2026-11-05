"""
Exact reduction of the routing graph to a *core*.

The raw noded graph has many shape points that are not municipal sources.
Removing a non-source node and replacing paths through it with exact shortcut
edges preserves every source-to-source shortest-path distance while making the
later Dijkstra runs much faster.

This version also exposes detailed per-pass progress and resumable checkpoints
through a callback supplied by ``run.py``. A checkpoint is written only after a
complete contraction pass, so an interruption can at worst repeat the current
pass; it never resumes from a half-mutated graph.
"""

from __future__ import annotations

from datetime import datetime
import threading
import time
from typing import Callable

import numpy as np

ProgressFn = Callable[[str], None]
CheckpointFn = Callable[[dict], None]


def _default_progress(message: str) -> None:
    print(message, flush=True)


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


class _Heartbeat:
    """Print a periodic message while one vectorised pass is still running."""

    def __init__(
        self,
        progress: ProgressFn,
        label: str,
        pass_no: int,
        interval_seconds: float,
    ) -> None:
        self.progress = progress
        self.label = label
        self.pass_no = pass_no
        self.interval = max(float(interval_seconds), 0.0)
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        if self.interval > 0:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            elapsed = time.monotonic() - self.started
            self.progress(
                f"    [{_stamp()}] {self.label}, pass {self.pass_no}: "
                f"still working ({elapsed:,.0f}s elapsed)"
            )

    def __exit__(self, exc_type, exc, tb):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        return False


# ---------------------------------------------------------------------------
def dedupe_edges(u, v, w, n_nodes):
    """Canonicalise to u < v, drop self-loops, keep the minimum parallel weight."""
    u = np.asarray(u, dtype=np.int64)
    v = np.asarray(v, dtype=np.int64)
    w = np.asarray(w, dtype=np.float64)

    lo = np.minimum(u, v)
    hi = np.maximum(u, v)
    keep = lo != hi
    lo, hi, w = lo[keep], hi[keep], w[keep]
    if lo.size == 0:
        return lo, hi, w

    key = lo * np.int64(n_nodes) + hi
    order = np.lexsort((w, key))
    key, lo, hi, w = key[order], lo[order], hi[order], w[order]
    first = np.empty(key.size, dtype=bool)
    first[0] = True
    np.not_equal(key[1:], key[:-1], out=first[1:])
    return lo[first], hi[first], w[first]


def _csr(u, v, w, n_nodes):
    """Undirected adjacency in CSR form: (indptr, neighbour, weight, degree)."""
    src = np.concatenate([u, v])
    dst = np.concatenate([v, u])
    wt = np.concatenate([w, w])
    order = np.argsort(src, kind="stable")
    src, dst, wt = src[order], dst[order], wt[order]
    deg = np.bincount(src, minlength=n_nodes)
    indptr = np.concatenate([[0], np.cumsum(deg)]).astype(np.int64)
    return indptr, dst, wt, deg


def _ragged_range(starts, lengths):
    """Concatenation of range(s, s + l) for each (s, l), without a Python loop."""
    starts = np.asarray(starts, np.int64)
    lengths = np.asarray(lengths, np.int64)
    total = int(lengths.sum())
    if total == 0:
        return np.empty(0, np.int64)
    offsets = np.concatenate([[0], np.cumsum(lengths)[:-1]])
    return np.repeat(starts - offsets, lengths) + np.arange(total, dtype=np.int64)


def _priority(nodes):
    """Deterministic pseudo-random priority (splitmix64 finaliser)."""
    x = nodes.astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15)
    x ^= x >> np.uint64(30)
    x *= np.uint64(0xBF58476D1CE4E5B9)
    x ^= x >> np.uint64(27)
    x *= np.uint64(0x94D049BB133111EB)
    x ^= x >> np.uint64(31)
    return x


def _initial_alive(u, v, n_nodes, source_nodes):
    is_source = np.zeros(n_nodes, dtype=bool)
    is_source[np.asarray(source_nodes, np.int64)] = True
    alive = np.zeros(n_nodes, dtype=bool)
    alive[u] = True
    alive[v] = True
    alive[is_source] = True
    return is_source, alive


def _contract_pass(u, v, w, n_nodes, is_source, max_degree, alive):
    """Contract one independent set. Returns ``(u, v, w, n_removed)``."""
    indptr, nbr, nwt, deg = _csr(u, v, w, n_nodes)

    removable = alive & ~is_source & (deg <= max_degree) & (deg > 0)
    if not removable.any():
        return u, v, w, 0

    prio = _priority(np.arange(n_nodes, dtype=np.int64))
    cand_nodes = np.flatnonzero(removable)
    owner = np.repeat(cand_nodes, deg[cand_nodes])
    flat = _ragged_range(indptr[cand_nodes], deg[cand_nodes])
    nb = nbr[flat]
    conflict = removable[nb] & (prio[nb] <= prio[owner])
    bad = np.zeros(n_nodes, dtype=bool)
    bad[owner[conflict]] = True
    chosen = cand_nodes[~bad[cand_nodes]]
    if chosen.size == 0:
        return u, v, w, 0

    su_l, sv_l, sw_l = [], [], []
    dsel = deg[chosen]
    for d in range(2, max_degree + 1):
        grp = chosen[dsel == d]
        if grp.size == 0:
            continue
        cols = indptr[grp][:, None] + np.arange(d, dtype=np.int64)
        nb_d = nbr[cols]
        wt_d = nwt[cols]
        ii, jj = np.triu_indices(d, k=1)
        su_l.append(nb_d[:, ii].ravel())
        sv_l.append(nb_d[:, jj].ravel())
        sw_l.append((wt_d[:, ii] + wt_d[:, jj]).ravel())
    if su_l:
        su = np.concatenate(su_l)
        sv = np.concatenate(sv_l)
        sw = np.concatenate(sw_l)
    else:
        su = sv = np.empty(0, np.int64)
        sw = np.empty(0, np.float64)

    gone = np.zeros(n_nodes, dtype=bool)
    gone[chosen] = True
    keep = ~(gone[u] | gone[v])
    u = np.concatenate([u[keep], su])
    v = np.concatenate([v[keep], sv])
    w = np.concatenate([w[keep], sw])
    u, v, w = dedupe_edges(u, v, w, n_nodes)
    alive[chosen] = False
    return u, v, w, chosen.size


def _phase_schedule(max_degree: int) -> list[tuple[int, str]]:
    schedule: list[tuple[int, str]] = [
        (1, "leaf pruning"),
        (2, "chain contraction"),
    ]
    if max_degree >= 3:
        for degree in range(3, max_degree + 1):
            schedule.append((degree, f"degree-{degree} contraction"))
            schedule.append((2, "chain contraction"))
    schedule.append((1, "leaf pruning"))
    return schedule


def reduce_to_core(
    u,
    v,
    w,
    n_nodes,
    source_nodes,
    max_degree=4,
    max_edge_growth=1.05,
    verbose=True,
    *,
    alive=None,
    best_edges=None,
    start_phase=0,
    start_pass=0,
    completed_passes=0,
    initial_nodes=None,
    initial_edges=None,
    checkpoint: CheckpointFn | None = None,
    heartbeat_seconds=30.0,
    progress: ProgressFn | None = None,
):
    """Contract away non-source nodes and optionally checkpoint every pass.

    The first seven parameters preserve the original API. The keyword-only
    resume parameters are used by ``run.py`` and can be ignored by callers that
    want a one-shot in-memory reduction.

    Checkpoints describe the *next* pass to run. Therefore, if the process is
    interrupted while a pass is executing, rerunning repeats that whole pass
    from the last complete, internally consistent graph state.
    """
    emit = progress or _default_progress
    u = np.asarray(u, np.int64)
    v = np.asarray(v, np.int64)
    w = np.asarray(w, np.float64)

    is_source, default_alive = _initial_alive(u, v, n_nodes, source_nodes)
    if alive is None or np.asarray(alive).size == 0:
        alive = default_alive
    else:
        alive = np.asarray(alive, dtype=bool).copy()
        if alive.shape != (n_nodes,):
            raise ValueError(f"alive mask has shape {alive.shape}; expected {(n_nodes,)}")
        alive[is_source] = True

    current_nodes = int(alive.sum())
    if initial_nodes is None:
        initial_nodes = current_nodes
    if initial_edges is None:
        initial_edges = int(u.size)
    if best_edges is None or int(best_edges) < 0:
        best_edges = int(u.size)
    else:
        best_edges = int(best_edges)

    schedule = _phase_schedule(int(max_degree))
    if not 0 <= int(start_phase) <= len(schedule):
        raise ValueError(f"invalid start_phase {start_phase}; schedule has {len(schedule)} phases")

    if verbose:
        if start_phase or start_pass or completed_passes:
            emit(
                f"  [{_stamp()}] resuming reduction at phase {start_phase + 1}/"
                f"{len(schedule)}, after {completed_passes:,} completed passes"
            )
        emit(
            f"  [{_stamp()}] reduction state: {current_nodes:,} live nodes, "
            f"{u.size:,} edges; original {initial_nodes:,} nodes, {initial_edges:,} edges"
        )

    if start_phase == len(schedule):
        return u, v, w, alive

    reduction_started = time.monotonic()

    for phase_index in range(int(start_phase), len(schedule)):
        degree, label = schedule[phase_index]
        pass_done_in_phase = int(start_pass) if phase_index == int(start_phase) else 0

        while True:
            pass_no = pass_done_in_phase + 1
            before_nodes = int(alive.sum())
            before_edges = int(u.size)
            pass_started = time.monotonic()

            if verbose:
                removed_pct = 100.0 * (initial_nodes - before_nodes) / max(initial_nodes, 1)
                emit(
                    f"    [{_stamp()}] phase {phase_index + 1}/{len(schedule)} "
                    f"{label}, pass {pass_no}: START; "
                    f"{before_nodes:,} nodes, {before_edges:,} edges, "
                    f"{removed_pct:.1f}% of original nodes removed"
                )

            with _Heartbeat(emit, label, pass_no, heartbeat_seconds if verbose else 0):
                u, v, w, removed = _contract_pass(
                    u, v, w, n_nodes, is_source, degree, alive
                )

            pass_seconds = time.monotonic() - pass_started
            completed_passes += 1
            pass_done_in_phase += 1
            best_edges = min(best_edges, int(u.size))

            growth_cap_hit = (
                degree >= 3
                and removed > 0
                and int(u.size) > best_edges * float(max_edge_growth)
            )
            phase_done = removed == 0 or growth_cap_hit
            next_phase = phase_index + 1 if phase_done else phase_index
            next_pass = 0 if phase_done else pass_done_in_phase

            if verbose:
                after_nodes = int(alive.sum())
                delta_edges = int(u.size) - before_edges
                total_seconds = time.monotonic() - reduction_started
                emit(
                    f"    [{_stamp()}] phase {phase_index + 1}/{len(schedule)} "
                    f"{label}, pass {pass_no}: DONE in {pass_seconds:,.1f}s; "
                    f"removed {removed:,} nodes; now {after_nodes:,} nodes, "
                    f"{u.size:,} edges ({delta_edges:+,}); "
                    f"reduction-session elapsed {total_seconds:,.1f}s"
                )
                if growth_cap_hit:
                    emit(
                        f"    [{_stamp()}] {label}: edge-growth cap hit "
                        f"({u.size:,} > {best_edges:,} × {max_edge_growth}); advancing"
                    )

            if checkpoint is not None:
                checkpoint(
                    {
                        "u": u,
                        "v": v,
                        "w": w,
                        "alive": alive,
                        "best_edges": best_edges,
                        "next_phase": next_phase,
                        "next_pass": next_pass,
                        "completed_passes": completed_passes,
                        "initial_nodes": int(initial_nodes),
                        "initial_edges": int(initial_edges),
                        "phase_done": bool(phase_done),
                    }
                )

            if phase_done:
                if verbose:
                    emit(
                        f"    [{_stamp()}] after {label}: "
                        f"{int(alive.sum()):,} nodes, {u.size:,} edges"
                    )
                break

    if verbose:
        n1 = int(alive.sum())
        emit(
            f"  [{_stamp()}] reduction done: {initial_nodes:,} -> {n1:,} nodes "
            f"({100 * n1 / max(initial_nodes, 1):.1f}%), "
            f"{initial_edges:,} -> {u.size:,} edges "
            f"({100 * u.size / max(initial_edges, 1):.1f}%)"
        )
    return u, v, w, alive


def compact(u, v, w, alive, source_nodes):
    """Relabel surviving nodes to 0..n-1."""
    keep = np.flatnonzero(alive)
    remap = np.full(alive.size, -1, dtype=np.int64)
    remap[keep] = np.arange(keep.size, dtype=np.int64)
    return remap[u], remap[v], w, keep.size, remap[np.asarray(source_nodes, np.int64)]
