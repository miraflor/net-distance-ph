"""
Many-to-many shortest paths: parallel, checkpointed, resumable, and ordered so
that near-neighbour distances land first.

Shape of the problem. There are ~1,600 sources and, after contraction, a core
graph of a few tens of thousands of nodes. One Dijkstra run from a source
settles every node within its radius, so a single run yields a whole row of the
matrix. The unit of work is therefore one source.

**Waves.** A Dijkstra call takes a distance ``limit``, and a limited run settles
only the nodes inside it -- measured at ~2% of the cost of an unlimited run for
a radius covering the first Euclidean neighbour ring. So the matrix is filled
in waves of increasing radius, the last of which is unlimited. Near pairs are
therefore known within seconds; far pairs arrive later. The bookkeeping that
makes this safe is a **per-row validity radius**:

    row i has radius R  =>  every finite entry in row i is exact and final,
                            and every infinite entry is known to exceed R.

When R reaches infinity the row is complete and infinity means genuinely
unreachable. The radius array is written alongside the matrix at every
checkpoint, so a partial result is never ambiguous.

The cost of the waves is real but small, because the cost of a limited run
scales roughly with the area swept: two cheap waves before the unlimited one
add on the order of 20% to the total. Set ``waves=()`` to pay nothing and get
no early output.

**On symmetry and why it buys no speed.** The graph is undirected with
non-negative weights, so d(i, j) = d(j, i) identically. It is tempting to
compute only the upper triangle. It does not help: the unit of work is a
source, not a pair, and one Dijkstra from i already delivers the entire row i
-- which by symmetry is also column i. Half the pairs is still all the rows.
Symmetry is used here as a correctness check instead. The one genuine residue
is that Dijkstra from i sums a path in one order and from j in the reverse, and
floating-point addition is not associative, so the two can differ in the last
bits; ``expand_and_finalise`` reports that and then makes it exactly zero.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import signal
import time
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra, reverse_cuthill_mckee

_G = None
_SRC = None


# ---------------------------------------------------------------------------
# Graph packaging
# ---------------------------------------------------------------------------
def build_csr(u, v, w, n_nodes):
    """Symmetric CSR from a de-duplicated undirected edge list.

    De-duplication is not optional: scipy's COO -> CSR conversion *sums*
    duplicate entries, so a surviving parallel edge would silently become an
    edge of the wrong weight. ``net_distance_ph.reduce.dedupe_edges`` guarantees it.

    Indices are stored as int32 when they fit, which is the layout scipy's
    csgraph wants internally -- it avoids a conversion on every call.
    """
    data = np.concatenate([w, w]).astype(np.float64)
    rows = np.concatenate([u, v])
    cols = np.concatenate([v, u])
    order = np.lexsort((cols, rows))
    rows, cols, data = rows[order], cols[order], data[order]
    indptr = np.concatenate([[0], np.cumsum(np.bincount(rows, minlength=n_nodes))])
    itype = np.int32 if max(n_nodes, data.size) < 2**31 - 1 else np.int64
    return csr_matrix(
        (data, cols.astype(itype), indptr.astype(itype)), shape=(n_nodes, n_nodes)
    )


def rcm_reorder(u, v, w, n_nodes, src_nodes, verbose=True):
    """Relabel nodes in reverse Cuthill-McKee order.

    Pure cache locality: RCM clusters each node's neighbours near it in index
    space, so Dijkstra's random-looking adjacency reads land in the same cache
    lines. Measured at 1.44x on a core graph of this shape. It is a permutation
    of node labels and changes no distance whatsoever.
    """
    G = build_csr(u, v, w, n_nodes)
    perm = reverse_cuthill_mckee(G, symmetric_mode=True)
    inv = np.empty(perm.size, dtype=np.int64)
    inv[perm] = np.arange(perm.size, dtype=np.int64)
    if verbose and u.size:
        old = float(np.abs(u.astype(np.int64) - v.astype(np.int64)).mean())
        new = float(np.abs(inv[u] - inv[v]).mean())
        print(f"  RCM reorder: mean |i-j| across edges {old:,.0f} -> {new:,.0f}")
    return inv[u], inv[v], w, inv[np.asarray(src_nodes, np.int64)]


def save_core(G, out_dir):
    """Write the CSR as three plain .npy files so workers can memory-map them."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "core_data.npy", G.data)
    np.save(out_dir / "core_indices.npy", G.indices)
    np.save(out_dir / "core_indptr.npy", G.indptr)
    np.save(out_dir / "core_shape.npy", np.array(G.shape, dtype=np.int64))


CORE_FILES = ("core_data.npy", "core_indices.npy", "core_indptr.npy", "core_shape.npy")


def core_fingerprint(work_dir, src_nodes):
    """SHA-256 of the core CSR arrays and the source-node list.

    The matrix checkpoint and the reports in ``out/`` record this value, so a
    checkpoint computed on a different core graph is never resumed and a
    stale ``out/`` is never verified as current. The arrays are read fully
    (not memory-mapped) so that no file stays locked on Windows.
    """
    work_dir = Path(work_dir)
    h = hashlib.sha256()
    for name in CORE_FILES:
        arr = np.ascontiguousarray(np.load(work_dir / name))
        h.update(f"{name}|{arr.dtype.str}|{arr.shape}|".encode())
        h.update(arr.tobytes())
    src = np.ascontiguousarray(np.asarray(src_nodes, dtype=np.int64))
    h.update(b"src_nodes|")
    h.update(src.tobytes())
    return h.hexdigest()


def load_core(work_dir, mmap=True):
    """Load the core CSR, memory-mapped so N workers share one copy in RAM."""
    work_dir = Path(work_dir)
    mode = "r" if mmap else None
    return csr_matrix(
        (
            np.load(work_dir / "core_data.npy", mmap_mode=mode),
            np.load(work_dir / "core_indices.npy", mmap_mode=mode),
            np.load(work_dir / "core_indptr.npy", mmap_mode=mode),
        ),
        shape=tuple(np.load(work_dir / "core_shape.npy")),
    )


def drop_unreachable(u, v, w, n_nodes, src_nodes, verbose=True):
    """Discard components containing no source: pure dead weight in the CSR."""
    G = build_csr(u, v, w, n_nodes)
    ncomp, label = connected_components(G, directed=False)
    keep_lab = np.zeros(ncomp, dtype=bool)
    keep_lab[label[src_nodes]] = True
    keep = keep_lab[label]
    remap = np.full(n_nodes, -1, np.int64)
    remap[keep] = np.arange(int(keep.sum()), dtype=np.int64)
    m = keep[u] & keep[v]
    if verbose:
        sizes = np.bincount(label[src_nodes], minlength=ncomp)
        print(
            f"  {ncomp:,} components; {int((sizes > 0).sum()):,} contain sources "
            f"(largest holds {sizes.max():,} of {len(src_nodes):,}); "
            f"dropped {n_nodes - int(keep.sum()):,} unreachable nodes"
        )
    return remap[u[m]], remap[v[m]], w[m], int(keep.sum()), remap[src_nodes]


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------
def _init(work_dir, src_nodes):
    global _G, _SRC
    _G = load_core(work_dir, mmap=True)
    _SRC = src_nodes


def _init_pool_worker(work_dir, src_nodes):
    # Ctrl-C in a terminal signals the whole process group. Only the parent
    # should react; it then terminates the pool. Without this, workers die
    # one by one, the pool replaces them, and the parent hangs in join().
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _init(work_dir, src_nodes)


def _close_worker_state():
    """Release memory-mapped CSR arrays held by the current process.

    This matters on Windows, where an open memory map prevents TemporaryDirectory
    (and other cleanup code) from deleting the underlying .npy files.
    """
    global _G, _SRC
    if _G is not None:
        for arr in (_G.data, _G.indices, _G.indptr):
            mmap = getattr(arr, "_mmap", None)
            if mmap is not None:
                mmap.close()
        _G = None
    _SRC = None


def _work(task):
    batch, limit = task
    d = dijkstra(_G, directed=False, indices=_SRC[batch], limit=limit)
    return batch, d[:, _SRC], limit


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------
class Checkpoint:
    """Matrix plus per-row validity radius on disk, written crash-safely.

    The matrix is a memmap, so absorbed rows are already on their way to disk;
    the radius array is the authority on what is trustworthy and is replaced
    atomically *after* the matrix is flushed. A crash in between leaves rows
    that are better than their recorded radius claims -- safe, because they
    will simply be recomputed.

    ``fingerprint`` (see :func:`core_fingerprint`) is stored beside the
    checkpoint. An existing checkpoint is resumed only if its fingerprint
    matches; otherwise it belongs to a different core graph and is discarded.
    Without this, a rerun after new inputs resumed old rows, and because rows
    are merged with ``np.minimum`` the old values could never be replaced.
    ``fingerprint=None`` skips the check (direct API use only).
    """

    def __init__(self, work_dir, S, fingerprint=None, verbose=False):
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mat_path = self.dir / "core_matrix.npy"
        self.rad_path = self.dir / "core_radius.npy"
        self.meta_path = self.dir / "core_matrix_meta.json"
        resume = self.mat_path.exists() and self.rad_path.exists()
        if resume and fingerprint is not None:
            try:
                recorded = json.loads(self.meta_path.read_text(encoding="utf-8")).get("core_fingerprint")
            except (OSError, ValueError, AttributeError):
                recorded = None
            if recorded != fingerprint:
                resume = False
                if verbose:
                    print(
                        "  existing matrix checkpoint was computed on a different core "
                        "graph (or has no fingerprint); discarding it"
                    )
        if resume:
            self.D = np.lib.format.open_memmap(self.mat_path, mode="r+")
            self.radius = np.load(self.rad_path)
            if self.D.shape != (S, S) or self.radius.shape != (S,):
                raise SystemExit(
                    f"Checkpoint is {self.D.shape} but there are {S} sources. "
                    "The source list changed; delete work/core_matrix.npy and "
                    "work/core_radius.npy and rerun."
                )
        else:
            # Remove the fingerprint first: a crash while the new checkpoint is
            # being created then leaves no fingerprint, so it is not resumed.
            self.meta_path.unlink(missing_ok=True)
            self.D = np.lib.format.open_memmap(
                self.mat_path, mode="w+", dtype=np.float64, shape=(S, S)
            )
            self.D[:] = np.inf
            self.radius = np.zeros(S, dtype=np.float64)
            self.flush()
            if fingerprint is not None:
                tmp = self.meta_path.with_name(self.meta_path.name + ".tmp")
                tmp.write_text(json.dumps({"core_fingerprint": fingerprint, "n_sources": int(S)}), encoding="utf-8")
                os.replace(tmp, self.meta_path)

    def absorb(self, batch, rows, limit):
        self.D[batch] = np.minimum(self.D[batch], rows)
        self.radius[batch] = np.maximum(self.radius[batch], limit)

    def flush(self):
        self.D.flush()
        tmp = self.rad_path.with_suffix(".tmp.npy")
        np.save(tmp, self.radius)
        os.replace(tmp, self.rad_path)

    def close(self):
        """Flush and close the matrix memory map explicitly.

        Relying on garbage collection is not sufficient on Windows because the
        file remains locked until the mmap handle itself is closed.
        """
        if getattr(self, "D", None) is not None:
            self.D.flush()
            mmap = getattr(self.D, "_mmap", None)
            if mmap is not None:
                mmap.close()
            self.D = None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def compute(
    work_dir,
    src_nodes,
    order=None,
    waves=(),
    n_workers=0,
    batch_size=8,
    checkpoint_every=8,
    on_wave_end=None,
    verbose=True,
):
    """Fill the source-to-source matrix, resuming from whatever is on disk.

    ``waves`` is a sequence of per-source radius arrays in km, applied in
    order; an unlimited wave is always appended last. ``order`` is the sequence
    in which sources are visited within each wave.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    if n_workers < 0:
        raise ValueError("n_workers must be 0 (automatic) or positive")
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be a positive integer")
    work_dir = Path(work_dir)
    src_nodes = np.asarray(src_nodes, dtype=np.int64)
    S = src_nodes.size
    ck = Checkpoint(work_dir, S, fingerprint=core_fingerprint(work_dir, src_nodes), verbose=verbose)

    order = np.arange(S, dtype=np.int64) if order is None else np.asarray(order, np.int64)
    schedule = [np.asarray(r, dtype=np.float64) for r in waves]
    schedule.append(np.full(S, np.inf))

    n_workers = max(1, n_workers or max(1, (os.cpu_count() or 2) - 1))
    pool = None
    # Use spawn on all platforms: scientific libraries and notebooks may have
    # background threads, making fork unsafe even on Linux. Workers receive
    # immutable disk-backed graph state through the initializer, so this does
    # not change the routing algorithm or any computed distances.
    ctx = mp.get_context("spawn")
    if verbose:
        print(f"  {S:,} sources, {len(schedule)} waves, {n_workers} worker(s)")
    result_D = None
    result_radius = None
    try:
        if n_workers > 1:
            pool = ctx.Pool(
                n_workers, initializer=_init_pool_worker, initargs=(str(work_dir), src_nodes)
            )
        else:
            _init(str(work_dir), src_nodes)

        for wi, target in enumerate(schedule):
            unlimited = bool(np.isinf(target).all())
            label = "unlimited" if unlimited else f"wave {wi + 1}"
            todo = order[ck.radius[order] < target[order]]
            if todo.size == 0:
                if verbose:
                    print(f"    [{label}] already satisfied")
                continue

            # scipy takes one limit per Dijkstra call, so a batch uses the
            # widest radius any of its members needs. Locality ordering is what
            # keeps that from being dominated by a single far-flung outlier.
            tasks = [
                (todo[s : s + batch_size], float(target[todo[s : s + batch_size]].max()))
                for s in range(0, todo.size, batch_size)
            ]

            t0 = time.time()
            done = 0
            it = pool.imap_unordered(_work, tasks) if pool else map(_work, tasks)
            for k, (b, rows, lim) in enumerate(it, 1):
                ck.absorb(b, rows, lim)
                done += b.size
                if k % checkpoint_every == 0:
                    ck.flush()
                if verbose:
                    rate = done / max(time.time() - t0, 1e-9)
                    print(
                        f"    [{label}] {done:,}/{todo.size:,} rows  {rate:6.1f} rows/s"
                        f"  eta {(todo.size - done) / max(rate, 1e-9):5.0f}s   ",
                        end="\r",
                        flush=True,
                    )
            ck.flush()
            if verbose:
                res = np.isfinite(np.asarray(ck.D)[todo]).sum(axis=1)
                print(
                    f"\n    [{label}] {todo.size:,} rows in {time.time() - t0:.1f}s; "
                    f"median {int(np.median(res)):,}/{S:,} towns resolved per row"
                )
            if on_wave_end is not None:
                on_wave_end(wi, label, np.asarray(ck.D), ck.radius)

        # Return ordinary in-memory arrays, not views backed by the memmap.
        # A view would keep the .npy file locked on Windows after compute exits.
        result_D = np.array(ck.D, copy=True)
        result_radius = ck.radius.copy()
    except BaseException:
        # Interrupted (Ctrl-C) or failed: stop the workers now instead of
        # waiting for every queued task, then save the rows already absorbed.
        # Saving is safe: a row's radius is raised only after its values.
        if pool is not None:
            pool.terminate()
            pool.join()
            pool = None
        try:
            ck.flush()
        except Exception:
            pass
        raise
    finally:
        if pool is not None:
            pool.close()
            pool.join()
        else:
            _close_worker_state()
        ck.close()

    return result_D, result_radius


# ---------------------------------------------------------------------------
def expand_and_finalise(
    D_core, row_radius, inv, offset_km, snapped, include_snap_offset=True, verbose=True
):
    """Lift the unique-node matrix to town space and make it exactly symmetric.

    Returns ``(D, town_radius, report)``. ``town_radius`` carries the validity
    radius through, so a partial matrix stays interpretable: a finite entry is
    final; an infinite entry means "further than the row's radius", which is
    genuine unreachability only once that radius is infinite.
    """
    N = inv.size
    D = np.full((N, N), np.inf)
    ok = np.flatnonzero(snapped)
    D[np.ix_(ok, ok)] = D_core[np.ix_(inv[ok], inv[ok])]

    town_radius = np.zeros(N)
    town_radius[ok] = row_radius[inv[ok]]
    town_radius[~snapped] = np.inf  # never reached the graph; its row is settled

    if include_snap_offset:
        # The last mile from centroid to network, charged at both ends.
        # Straight-line, hence a slight under-estimate of the true stub, but
        # symmetric by construction so it cannot break d(i, j) = d(j, i).
        add = offset_km[:, None] + offset_km[None, :]
        finite = np.isfinite(D)
        D[finite] += add[finite]

    fin = np.isfinite(D) & np.isfinite(D.T)
    asym = float(np.max(np.abs(D[fin] - D.T[fin]))) if fin.any() else 0.0
    scale = float(np.max(D[fin])) if fin.any() else 1.0

    # Symmetric by construction; the elementwise minimum removes the last-bit
    # residue from float summation order, and mid-run it also repairs the
    # window where rows i and j happen to have different validity radii.
    D = np.minimum(D, D.T)
    np.fill_diagonal(D, 0.0)

    complete = int(np.isinf(town_radius).sum())
    report = {
        "n_towns": int(N),
        "n_snapped": int(snapped.sum()),
        "n_rows_complete": complete,
        "fraction_complete": complete / max(N, 1),
        "max_asymmetry_km": asym,
        "max_asymmetry_relative": asym / max(scale, 1e-12),
        "n_pairs_resolved": int(np.isfinite(D).sum()),
        "n_pairs_unresolved_or_unreachable": int(np.isinf(D).sum()),
        "max_finite_km": float(D[np.isfinite(D)].max()) if np.isfinite(D).any() else 0.0,
    }
    if verbose:
        print(f"  rows complete: {complete:,}/{N:,}")
        print(
            f"  max |d(i,j) - d(j,i)| before symmetrising: {asym:.3e} km "
            f"({report['max_asymmetry_relative']:.2e} relative)"
        )
        print(f"  pairs resolved: {report['n_pairs_resolved']:,} of {N * N:,}")
    return D, town_radius, report
