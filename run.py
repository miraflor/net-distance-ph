#!/usr/bin/env python3
"""
Road + ferry shortest-path distance matrix between Philippine municipal
centroids.

    python run.py all --data DIR --workers 8
    python run.py graph --data DIR       # or run the stages one at a time
    python run.py reduce                 # later stages reuse the folder `graph` read
    python run.py matrix --workers 8     # resumable: just rerun after a crash
    python run.py verify

Useful flags on `matrix` (and `all`):

    --workers N        number of worker processes            (default: cores - 1)
    --batch N          sources per Dijkstra call             (default: 8)
    --waves 1,2        Euclidean neighbour orders to prioritise; "" disables
    --wave-scale F     inflate wave radii by F               (default: 1.5)
    --no-partial       do not rewrite out/ after every wave

Interrupting `matrix` at any point is safe. out/ always holds the best matrix
computed so far, and out/row_validity_radius_km.npy says how far each row is
trustworthy to: a finite entry is exact and final; an infinite entry means the
pair is further apart than that row's radius. When the radius is infinite the
row is complete and infinity means genuinely unreachable.

Each stage records what it was built from, and later stages refuse to mix
files from different runs: changed stage-1 settings in config.py, a stage-2
core built from older stage-1 files, a matrix checkpoint from another core,
or an out/ folder from another core are all detected.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as C
from ph_net_distance import graphbuild as gb
from ph_net_distance import matrix as mx
from ph_net_distance import order as od
from ph_net_distance import reduce as rd
from ph_net_distance.shpio import dbf_fields, read_dbf, read_shapefile_parts, read_shapefile_points
from ph_net_distance.provenance import shapefile_manifest

ID_PAT = re.compile(r"psgc|adm.*code|^code$|^id$|mun.*code|city.*code", re.I)
NAME_PAT = re.compile(r"name|adm.*en|mun|city|nam", re.I)

# True when the user named the input folder (--data or the environment
# variable). Otherwise the later stages reuse the folder recorded by `graph`.
DATA_EXPLICIT = "PH_NET_DISTANCE_DATA" in os.environ


class InputError(Exception):
    """An input shapefile cannot be resolved to exactly one file."""


def _locate(filename: str) -> Path:
    """Resolve an input unambiguously, avoiding accidental mixed snapshots.

    Raises :class:`InputError` (not SystemExit), so that check_setup.py can
    report every file instead of stopping at the first problem.
    """
    root = Path(C.DATA).expanduser()
    if not root.is_dir():
        raise InputError(
            f"Input directory does not exist: {root}. "
            "Set PH_NET_DISTANCE_DATA or use --data to locate the shapefiles."
        )
    pointers = {
        C.ROAD_SHP: ("current_transport.json", "roads"),
        C.FERRY_SHP: ("current_transport.json", "ferries"),
        C.CENTROID_SHP: ("current_boundaries.json", "centroids"),
    }
    if filename in pointers:
        pointer_name, key = pointers[filename]
        pointer = root / pointer_name
        if pointer.exists():
            try:
                record = json.loads(pointer.read_text(encoding="utf-8"))
                relative = Path(record[key])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("path must be relative to data root")
                chosen = (root / relative).resolve()
                chosen.relative_to(root.resolve())
                if chosen.name != filename or not chosen.is_file():
                    raise ValueError(f"missing selected file: {chosen}")
            except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
                raise InputError(f"Invalid {pointer}: {exc}") from exc
            return chosen
    hits = sorted(root.rglob(filename))
    if not hits:
        raise InputError(f"Missing {filename} beneath {root}.")
    if len(hits) > 1:
        choices = "\n".join(f"  - {p}" for p in hits)
        raise InputError(
            f"Ambiguous {filename}: {len(hits)} matching files beneath {root}.\n"
            f"{choices}\nUse a more specific --data directory."
        )
    return hits[0]


def _find(filename: str) -> Path:
    """Command-line form of :func:`_locate`: stop with a message."""
    try:
        return _locate(filename)
    except InputError as exc:
        raise SystemExit(f"{exc}\nTo diagnose: python check_setup.py --data DIR") from None


def _current_inputs():
    """Resolve all three sources, honoring committed dataset release pointers."""
    return {
        "roads": _locate(C.ROAD_SHP),
        "ferries": _locate(C.FERRY_SHP),
        "localities": _locate(C.CENTROID_SHP),
    }


def _input_manifest():
    return shapefile_manifest(_current_inputs(), Path(C.DATA))


def _adopt_recorded_data_root():
    """Later stages: reuse the input folder that `graph` read.

    The byte-level input check below must read the shapefiles again. Without
    this, `python run.py reduce` (as documented, without --data) looked in
    the default data/ folder and stopped. An explicit --data or
    PH_NET_DISTANCE_DATA still takes precedence.
    """
    with np.load(Path(C.WORK) / "sources.npz") as s:
        recorded = str(s["data_root"]) if "data_root" in s.files else None
    if recorded and not DATA_EXPLICIT:
        C.DATA = Path(recorded)
    return recorded


def _pick_column(fields, pattern, fallback_idx=0, exclude=()):
    names = [f[0] for f in fields]
    if not names:
        raise ValueError("The DBF attribute table has no columns.")
    # ``exclude`` keeps the label from landing on the ID column: NAME_PAT
    # matches "mun", so a field such as MUN_CODE would otherwise be chosen.
    for n in [n for n in names if n not in exclude] or names:
        if pattern.search(n):
            return n
    return names[min(fallback_idx, len(names) - 1)]


def _validated_ids(column, id_col):
    """Locality codes as exact text; reject anything that is not."""
    values = np.asarray(column)
    if values.dtype != object:
        # read_dbf returns text only for character fields and for integer
        # numeric fields with no blanks. A float result means blanks or
        # decimals, and str() would give codes like '1380600000.0' or 'nan'.
        raise SystemExit(
            f"Centroid ID field {id_col!r} was read as {values.dtype} (a numeric field with "
            "blank or non-integer values). Store locality codes in a text (C) field."
        )
    ids = [str(value).strip() for value in values]
    empty = [i for i, x in enumerate(ids) if not x]
    if empty:
        raise SystemExit(f"Centroid ID field {id_col!r} is empty in {len(empty)} row(s), first at row {empty[0]}.")
    repeated = [key for key, count in Counter(ids).items() if count > 1]
    if repeated:
        raise SystemExit(f"Duplicate centroid IDs (examples): {repeated[:10]}")
    return ids


# Settings that stage 1 bakes into work/graph_full.npz and work/sources.npz.
STAGE1_CONFIG_KEYS = (
    "ROAD_SHP", "FERRY_SHP", "CENTROID_SHP",
    "CENTROID_ID_COL", "CENTROID_NAME_COL", "DBF_ENCODING",
    "SNAP_DECIMALS", "GEODESIC_BACKEND", "FERRY_CONNECT_KM",
    "FERRY_COST_MULTIPLIER", "FERRY_FIXED_PENALTY_KM",
    "PREFER_ROAD_SNAP", "SNAP_FALLBACK_TERMINALS_ONLY", "MAX_CENTROID_SNAP_KM",
)


def _stage1_config():
    return {key: getattr(C, key, None) for key in STAGE1_CONFIG_KEYS}


def _require_stage1_current():
    """Check both graph settings and byte-level provenance of all GIS inputs."""
    with np.load(Path(C.WORK) / "sources.npz") as s:
        recorded = json.loads(str(s["stage1_config"])) if "stage1_config" in s.files else None
    if recorded is None:
        raise SystemExit(
            "work/sources.npz has no record of its stage-1 settings (written by an older "
            "version). Rerun: python run.py graph"
        )
    current = json.loads(json.dumps(_stage1_config()))
    changed = sorted(k for k in set(recorded) | set(current) if recorded.get(k) != current.get(k))
    if changed:
        details = ", ".join(f"{k}: {recorded.get(k)!r} -> {current.get(k)!r}" for k in changed)
        raise SystemExit(
            f"config.py stage-1 settings changed after work/ was built ({details}).\n"
            "Rerun all stages from: python run.py graph"
        )
    with np.load(Path(C.WORK) / "sources.npz") as s:
        stored = str(s["input_manifest"]) if "input_manifest" in s.files else None
    if not stored:
        raise SystemExit("Source hashes are missing from the old graph. Rerun: python run.py graph")
    recorded_root = _adopt_recorded_data_root()
    try:
        current_inputs = _input_manifest()
    except (InputError, FileNotFoundError) as exc:
        raise SystemExit(
            f"Cannot re-read the input shapefiles to confirm they are unchanged: {exc}\n"
            f"Pass --data with the folder used for `graph`"
            + (f" ({recorded_root})." if recorded_root else ".")
        ) from None
    if json.loads(stored)["fingerprint"] != current_inputs["fingerprint"]:
        raise SystemExit(
            "Input shapefiles or their sidecars changed since graph construction. "
            "The existing work/ is stale. Rerun: python run.py graph"
        )


def _require_core_current(cs):
    """Refuse to mix a stage-2 core with stage-1 files from another run."""
    signature = _stage1_signature()
    try:
        recorded = {key: int(cs[f"lineage_{key}"]) for key in signature}
    except KeyError:
        raise SystemExit(
            "work/core_sources.npz has no record of the stage-1 files it was built from "
            "(written by an older version). Rerun: python run.py reduce"
        ) from None
    if recorded != signature:
        raise SystemExit(
            "The stage-2 core in work/ was built from different stage-1 files than the "
            "current work/graph_full.npz and work/sources.npz.\nRerun: python run.py reduce"
        )


def _stamp():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _atomic_savez(path, **arrays):
    """Write a NumPy archive and atomically replace the previous checkpoint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.savez(fh, **arrays)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_write(path, writer, binary=False):
    """Write through a temporary file and rename, so readers never see a torn file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    opener = (lambda: open(tmp, "wb")) if binary else (lambda: open(tmp, "w", encoding="utf-8", newline=""))
    with opener() as fh:
        writer(fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _stage1_signature():
    graph_path = Path(C.WORK) / "graph_full.npz"
    source_path = Path(C.WORK) / "sources.npz"
    gs, ss = graph_path.stat(), source_path.stat()
    return {
        "graph_size": gs.st_size,
        "graph_mtime_ns": gs.st_mtime_ns,
        "sources_size": ss.st_size,
        "sources_mtime_ns": ss.st_mtime_ns,
    }


def _reduce_input_signature():
    return {
        **_stage1_signature(),
        "max_degree": int(C.MAX_CONTRACT_DEGREE),
        "max_edge_growth": float(C.MAX_EDGE_GROWTH),
    }


def _checkpoint_matches(cp, signature):
    try:
        return (
            int(cp["checkpoint_version"]) == 1
            and int(cp["graph_size"]) == signature["graph_size"]
            and int(cp["graph_mtime_ns"]) == signature["graph_mtime_ns"]
            and int(cp["sources_size"]) == signature["sources_size"]
            and int(cp["sources_mtime_ns"]) == signature["sources_mtime_ns"]
            and int(cp["max_degree"]) == signature["max_degree"]
            and float(cp["max_edge_growth"]) == signature["max_edge_growth"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _make_reduce_logger(log_path):
    lock = threading.Lock()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message):
        with lock:
            print(message, flush=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(message + "\n")
                fh.flush()
    return log


# ===========================================================================
def stage_graph(args):
    t0 = time.time()
    Path(C.WORK).mkdir(parents=True, exist_ok=True)
    print("[1/4] building the road + ferry graph")

    try:
        inputs = _current_inputs()
    except InputError as exc:
        raise SystemExit(f"{exc}\nTo diagnose: python check_setup.py --data DIR") from None
    input_manifest = shapefile_manifest(inputs, Path(C.DATA))
    road_p, ferry_p, cent_p = inputs["roads"], inputs["ferries"], inputs["localities"]
    print(f"  source fingerprint {input_manifest['fingerprint']}")
    print(f"  roads     {road_p}")
    print(f"  ferries   {ferry_p}")
    print(f"  centroids {cent_p}")

    roads = read_shapefile_parts(road_p)
    ferries = read_shapefile_parts(ferry_p)
    print(f"  {roads.n_records:,} road ways, {ferries.n_records:,} ferry ways")

    u, v, w, code, node_lonlat, flags = gb.build_graph(
        [(roads, gb.ROAD), (ferries, gb.FERRY)],
        snap_decimals=C.SNAP_DECIMALS,
        geodesic_backend=C.GEODESIC_BACKEND,
    )
    u, v, w, code = gb.connect_ferries(
        u, v, w, code, node_lonlat, flags, ferry_connect_km=C.FERRY_CONNECT_KM,
        geodesic_backend=C.GEODESIC_BACKEND,
    )
    w = gb.apply_ferry_cost(w, code, C.FERRY_COST_MULTIPLIER, C.FERRY_FIXED_PENALTY_KM)

    cent_xy = read_shapefile_points(cent_p)
    fields = dbf_fields(cent_p.with_suffix(".dbf"))
    id_col = C.CENTROID_ID_COL or _pick_column(fields, ID_PAT)
    name_col = C.CENTROID_NAME_COL or _pick_column(fields, NAME_PAT, 1, exclude=(id_col,))
    print(f"  centroid id column {id_col!r}, name column {name_col!r}")
    tbl, n_rec = read_dbf(cent_p.with_suffix(".dbf"), [id_col, name_col], encoding=C.DBF_ENCODING)
    if n_rec != cent_xy.shape[0]:
        raise SystemExit(f"{n_rec} dbf rows but {cent_xy.shape[0]} geometries")
    print(f"  {n_rec:,} municipalities/cities")

    # ID order defines every matrix row and column. Reject ambiguous IDs
    # before writing large intermediates; never coerce codes to numbers.
    identifiers = _validated_ids(tbl[id_col], id_col)
    names = [str(x).strip() for x in tbl[name_col]]
    if not np.isfinite(cent_xy).all() or (np.abs(cent_xy[:, 0]) > 180).any() or (np.abs(cent_xy[:, 1]) > 90).any():
        raise SystemExit("Invalid centroid longitude/latitude; require EPSG:4326 degrees.")

    node_idx, offset, tier = gb.snap_sources(
        cent_xy, node_lonlat, flags,
        max_snap_km=C.MAX_CENTROID_SNAP_KM, prefer_road=C.PREFER_ROAD_SNAP,
        fallback_terminals_only=C.SNAP_FALLBACK_TERMINALS_ONLY,
        geodesic_backend=C.GEODESIC_BACKEND,
    )
    if not (tier >= 0).any():
        raise SystemExit(
            f"No locality point is within MAX_CENTROID_SNAP_KM = {C.MAX_CENTROID_SNAP_KM} km of the "
            "network. Check that all three layers are WGS84 longitude/latitude."
        )

    np.savez_compressed(
        Path(C.WORK) / "graph_full.npz",
        u=u, v=v, w=w, code=code, node_lonlat=node_lonlat, flags=flags,
    )
    # Text arrays take the width of their longest value: a fixed width such as
    # U32 would silently truncate longer IDs and could merge distinct ones.
    np.savez(
        Path(C.WORK) / "sources.npz",
        node_idx=node_idx, offset=offset, tier=tier, lonlat=cent_xy,
        ids=np.array(identifiers, dtype=str),
        names=np.array(names, dtype=str),
        stage1_config=np.array(json.dumps(_stage1_config(), sort_keys=True)),
        input_manifest=np.array(json.dumps(input_manifest, sort_keys=True)),
        # Where the inputs were read; later stages reuse it (not part of the
        # fingerprint, so moving an identical data folder stays valid).
        data_root=np.array(str(Path(C.DATA).expanduser().resolve())),
    )
    print(f"  stage 1 done in {time.time() - t0:.1f}s")


# ===========================================================================
def stage_reduce(args):
    t0 = time.time()
    work = Path(C.WORK)
    work.mkdir(parents=True, exist_ok=True)
    checkpoint_path = work / "reduce_checkpoint.npz"
    log_path = work / "reduce_progress.log"
    log = _make_reduce_logger(log_path)

    log("")
    log(f"[2/4] reducing to a core graph — session started {_stamp()}")
    log(f"  progress log: {log_path}")
    log(
        f"  heartbeat every {C.REDUCE_HEARTBEAT_SECONDS:g}s; "
        f"checkpoint every {C.REDUCE_CHECKPOINT_EVERY_PASSES} completed pass(es)"
    )

    graph_path = work / "graph_full.npz"
    source_path = work / "sources.npz"
    if not graph_path.exists() or not source_path.exists():
        raise SystemExit("Stage 1 files are missing. Run: python run.py graph")
    _require_stage1_current()

    signature = _reduce_input_signature()
    if args.restart_reduce and checkpoint_path.exists():
        checkpoint_path.unlink()
        log("  --restart-reduce: deleted the existing stage-2 checkpoint")

    with np.load(graph_path) as g, np.load(source_path) as s:
        node_lonlat = g["node_lonlat"]
        n_nodes_original = node_lonlat.shape[0]
        snapped = s["tier"] >= 0
        src_orig, inv = np.unique(s["node_idx"][snapped], return_inverse=True)
        src_lonlat = node_lonlat[src_orig]
        log(f"  {int(snapped.sum()):,} snapped towns on {src_orig.size:,} distinct nodes")

        resume = False
        if checkpoint_path.exists():
            with np.load(checkpoint_path, allow_pickle=False) as cp:
                if _checkpoint_matches(cp, signature):
                    u = cp["u"].copy()
                    v = cp["v"].copy()
                    w = cp["w"].copy()
                    alive = cp["alive"].copy()
                    src = cp["src"].copy()
                    n_nodes = int(cp["n_nodes"])
                    best_edges = int(cp["best_edges"])
                    start_phase = int(cp["next_phase"])
                    start_pass = int(cp["next_pass"])
                    completed_passes = int(cp["completed_passes"])
                    initial_nodes = int(cp["initial_nodes"])
                    initial_edges = int(cp["initial_edges"])
                    resume = True
                    checkpoint_nodes = int(alive.sum()) if alive.size else n_nodes
                    log(
                        f"  [{_stamp()}] valid checkpoint found: "
                        f"{checkpoint_nodes:,} live nodes, {u.size:,} edges, "
                        f"{completed_passes:,} completed passes"
                    )
                else:
                    log("  existing checkpoint does not match the current stage-1 files/config; ignoring it")
            if not resume:
                checkpoint_path.unlink(missing_ok=True)

        if not resume:
            u, v, w = g["u"].copy(), g["v"].copy(), g["w"].copy()
            n_nodes = n_nodes_original
            log(f"  [{_stamp()}] de-duplicating {u.size:,} raw edges...")
            op = time.time()
            u, v, w = rd.dedupe_edges(u, v, w, n_nodes)
            log(f"  [{_stamp()}] de-duplication done: {u.size:,} edges in {time.time() - op:.1f}s")

            log(f"  [{_stamp()}] dropping components that contain no snapped town...")
            op = time.time()
            u, v, w, n_nodes, src = mx.drop_unreachable(
                u, v, w, n_nodes, src_orig, verbose=True
            )
            log(
                f"  [{_stamp()}] component filtering done: {n_nodes:,} nodes, "
                f"{u.size:,} edges in {time.time() - op:.1f}s"
            )
            alive = np.empty(0, dtype=bool)
            best_edges = -1
            start_phase = 0
            start_pass = 0
            completed_passes = 0
            initial_nodes = None
            initial_edges = None

            # Save the post-filter state before the first contraction pass. If
            # the laptop sleeps or the process is killed during that first pass,
            # the expensive loading/de-duplication/filtering work is retained.
            _atomic_savez(
                checkpoint_path,
                checkpoint_version=np.int64(1),
                **{k: np.asarray(vv) for k, vv in signature.items()},
                u=u, v=v, w=w, alive=alive, src=src,
                n_nodes=np.int64(n_nodes), best_edges=np.int64(-1),
                next_phase=np.int64(0), next_pass=np.int64(0),
                completed_passes=np.int64(0),
                initial_nodes=np.int64(-1), initial_edges=np.int64(-1),
            )
            log(f"  [{_stamp()}] initial stage-2 checkpoint written")

    checkpoint_counter = 0

    def save_pass(state):
        nonlocal checkpoint_counter
        checkpoint_counter += 1
        every = max(int(C.REDUCE_CHECKPOINT_EVERY_PASSES), 1)
        must_save = state["phase_done"] or checkpoint_counter % every == 0
        if not must_save:
            return
        started = time.time()
        log(
            f"    [{_stamp()}] saving atomic stage-2 checkpoint "
            f"after pass {state['completed_passes']:,}..."
        )
        _atomic_savez(
            checkpoint_path,
            checkpoint_version=np.int64(1),
            **{k: np.asarray(vv) for k, vv in signature.items()},
            u=state["u"], v=state["v"], w=state["w"],
            alive=state["alive"], src=src, n_nodes=np.int64(n_nodes),
            best_edges=np.int64(state["best_edges"]),
            next_phase=np.int64(state["next_phase"]),
            next_pass=np.int64(state["next_pass"]),
            completed_passes=np.int64(state["completed_passes"]),
            initial_nodes=np.int64(state["initial_nodes"]),
            initial_edges=np.int64(state["initial_edges"]),
        )
        log(f"    [{_stamp()}] checkpoint saved in {time.time() - started:.1f}s")

    u, v, w, alive = rd.reduce_to_core(
        u, v, w, n_nodes, src,
        max_degree=C.MAX_CONTRACT_DEGREE,
        max_edge_growth=C.MAX_EDGE_GROWTH,
        alive=alive,
        best_edges=best_edges,
        start_phase=start_phase,
        start_pass=start_pass,
        completed_passes=completed_passes,
        initial_nodes=None if initial_nodes is None or initial_nodes < 0 else initial_nodes,
        initial_edges=None if initial_edges is None or initial_edges < 0 else initial_edges,
        checkpoint=save_pass,
        heartbeat_seconds=C.REDUCE_HEARTBEAT_SECONDS,
        progress=log,
    )

    log(f"  [{_stamp()}] compacting surviving node IDs...")
    op = time.time()
    cu, cv, cw, cn, csrc = rd.compact(u, v, w, alive, src)
    log(f"  [{_stamp()}] compaction done: {cn:,} nodes, {cu.size:,} edges in {time.time() - op:.1f}s")

    if C.RCM_REORDER:
        log(f"  [{_stamp()}] applying reverse Cuthill-McKee reordering...")
        op = time.time()
        cu, cv, cw, csrc = mx.rcm_reorder(cu, cv, cw, cn, csrc)
        log(f"  [{_stamp()}] RCM reordering done in {time.time() - op:.1f}s")

    log(f"  [{_stamp()}] writing core CSR arrays...")
    op = time.time()
    mx.save_core(mx.build_csr(cu, cv, cw, cn), C.WORK)
    # lineage_*: which stage-1 files this core was built from; checked by the
    # matrix and verify stages (see _require_core_current).
    np.savez(
        work / "core_sources.npz",
        src_nodes=csrc, inv=inv, snapped=snapped, src_lonlat=src_lonlat,
        **{f"lineage_{key}": np.int64(signature[key]) for key in _stage1_signature()},
    )
    log(f"  [{_stamp()}] core files written in {time.time() - op:.1f}s")

    checkpoint_path.unlink(missing_ok=True)
    log("  stage-2 checkpoint removed because the stage completed successfully")
    log(f"  stage 2 done in {time.time() - t0:.1f}s")


# ===========================================================================
def _write_outputs(D_core, radius, s, cs, tag, with_csv, fingerprint):
    out = Path(C.OUT)
    out.mkdir(parents=True, exist_ok=True)
    snapped, inv = cs["snapped"], cs["inv"]
    N = snapped.size
    inv_full = np.zeros(N, dtype=np.int64)
    inv_full[np.flatnonzero(snapped)] = inv

    D, town_radius, report = mx.expand_and_finalise(
        D_core, radius, inv_full, s["offset"], snapped,
        include_snap_offset=C.INCLUDE_SNAP_OFFSET, verbose=with_csv,
    )
    report["stage"] = tag
    report["include_snap_offset"] = bool(C.INCLUDE_SNAP_OFFSET)
    report["core_fingerprint"] = fingerprint
    input_manifest = json.loads(str(s["input_manifest"]))
    report["input_fingerprint"] = input_manifest["fingerprint"]
    report["source_manifest"] = "source_manifest.json"
    # Write the manifest for each partial/final publish; content is immutable
    # after graph creation and never depends on progress of a matrix wave.
    _atomic_write(out / "source_manifest.json", lambda fh: fh.write(json.dumps({
        "input_manifest": input_manifest,
        "graph_settings": json.loads(str(s["stage1_config"])),
        "core_fingerprint": fingerprint,
        "include_snap_offset": bool(C.INCLUDE_SNAP_OFFSET),
    }, indent=2) + "\n"))

    # Every file is replaced atomically, so an interruption never leaves a
    # truncated matrix in out/. The matrix goes first and the radius after
    # it: an older radius with a newer matrix still reads correctly.
    _atomic_write(out / "distance_matrix_km.npy", lambda fh: np.save(fh, D), binary=True)
    _atomic_write(out / "row_validity_radius_km.npy", lambda fh: np.save(fh, town_radius), binary=True)
    _atomic_write(out / "matrix_report.json", lambda fh: fh.write(json.dumps(report, indent=2) + "\n"))

    if with_csv:
        ids = [str(x) for x in s["ids"]]
        names = [str(x) for x in s["names"]]

        def matrix_csv(fh):
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(["id", *ids])
            for i in range(N):
                writer.writerow([ids[i], *("" if not np.isfinite(x) else f"{x:.4f}" for x in D[i])])

        ll, off, tier = s["lonlat"], s["offset"], s["tier"]

        def towns_csv(fh):
            # The csv module quotes and escapes as needed, so an ID or name that
            # contains a comma or a double quote is kept exactly.
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(["row", "id", "name", "lon", "lat", "snap_tier", "snap_offset_km", "validity_radius_km"])
            for i in range(N):
                o = "" if not np.isfinite(off[i]) else f"{off[i]:.4f}"
                r = "inf" if np.isinf(town_radius[i]) else f"{town_radius[i]:.3f}"
                writer.writerow([i, ids[i], names[i], f"{ll[i, 0]:.6f}", f"{ll[i, 1]:.6f}", int(tier[i]), o, r])

        _atomic_write(out / "distance_matrix_km.csv", matrix_csv)
        _atomic_write(out / "towns.csv", towns_csv)
    return report


def stage_matrix(args):
    t0 = time.time()
    print("[3/4] computing the distance matrix")
    _require_stage1_current()
    cs = np.load(Path(C.WORK) / "core_sources.npz")
    s = np.load(Path(C.WORK) / "sources.npz")
    _require_core_current(cs)
    src_nodes = cs["src_nodes"]
    fingerprint = mx.core_fingerprint(C.WORK, src_nodes)

    wave_orders = args.waves if args.waves is not None else list(C.WAVE_ORDERS)
    scale = args.wave_scale if args.wave_scale is not None else C.WAVE_SCALE

    order = None
    waves = ()
    if C.SOURCE_ORDER == "knn_bfs" or wave_orders:
        E = od.euclidean_matrix(cs["src_lonlat"])
        A = od.knn_graph(E, k=C.KNN_K)
        if C.SOURCE_ORDER == "knn_bfs":
            order = od.bfs_order(A, cs["src_lonlat"])
        if wave_orders:
            waves = od.wave_radii(E, A, orders=wave_orders, scale=scale)

    def _on_wave_end(wi, label, D_core, radius):
        if C.WRITE_PARTIAL_EACH_WAVE:
            rep = _write_outputs(D_core, radius, s, cs, f"after {label}", with_csv=False, fingerprint=fingerprint)
            print(
                f"    [{label}] wrote partial output: "
                f"{rep['n_pairs_resolved']:,} pairs resolved, "
                f"{rep['n_rows_complete']:,}/{rep['n_towns']:,} rows complete"
            )

    D_core, radius = mx.compute(
        C.WORK, src_nodes,
        order=order, waves=waves,
        n_workers=args.workers if args.workers is not None else C.N_WORKERS,
        batch_size=args.batch if args.batch is not None else C.BATCH_SIZE,
        checkpoint_every=C.CHECKPOINT_EVERY,
        on_wave_end=None if args.no_partial else _on_wave_end,
    )

    _write_outputs(D_core, radius, s, cs, "final", with_csv=True, fingerprint=fingerprint)
    print(f"  wrote {Path(C.OUT) / 'distance_matrix_km.npy'} and .csv")
    print(f"  stage 3 done in {time.time() - t0:.1f}s")


# ===========================================================================
def stage_verify(args):
    """Independent check that the fast path gave the same answer as the slow one."""
    from scipy.sparse.csgraph import dijkstra

    print("[4/4] verifying")
    lines = []
    failures = []

    def check(ok, message):
        lines.append(("PASS  " if ok else "FAIL  ") + message)
        if not ok:
            failures.append(message)

    # The files under test must belong together; otherwise every comparison
    # below would be meaningless.
    _require_stage1_current()
    s = np.load(Path(C.WORK) / "sources.npz")
    cs = np.load(Path(C.WORK) / "core_sources.npz")
    _require_core_current(cs)
    report = json.loads((Path(C.OUT) / "matrix_report.json").read_text(encoding="utf-8"))
    if report.get("input_fingerprint") != json.loads(str(s["input_manifest"]))["fingerprint"]:
        raise SystemExit("out/ source fingerprint differs from work/; rerun: python run.py matrix")
    manifest_file = Path(C.OUT) / "source_manifest.json"
    if not manifest_file.is_file():
        raise SystemExit("out/source_manifest.json is missing; rerun: python run.py matrix")
    published = json.loads(manifest_file.read_text(encoding="utf-8"))
    if published.get("input_manifest", {}).get("fingerprint") != report["input_fingerprint"]:
        raise SystemExit("out/ source manifest does not match its matrix; rerun: python run.py matrix")
    if report.get("core_fingerprint") != mx.core_fingerprint(C.WORK, cs["src_nodes"]):
        raise SystemExit(
            "out/ was not produced from the current core graph in work/. "
            "Rerun: python run.py matrix"
        )
    D = np.load(Path(C.OUT) / "distance_matrix_km.npy")
    town_radius = np.load(Path(C.OUT) / "row_validity_radius_km.npy")
    if D.shape != (s["ids"].size, s["ids"].size):
        raise SystemExit(f"out/ matrix is {D.shape} but work/sources.npz has {s['ids'].size} towns")

    complete = int(np.isinf(town_radius).sum())
    lines.append(f"output stage: {report.get('stage')}; rows complete: {complete:,}/{len(town_radius):,}")
    if complete < len(town_radius):
        lines.append("NOTE: matrix is partial; checks below apply to what is resolved")

    # Symmetry. out/ is made exactly symmetric by construction, so the useful
    # number is the asymmetry measured *before* that step (matrix_report.json).
    rel = float(report["max_asymmetry_relative"])
    check(
        rel <= C.VERIFY_MAX_ASYMMETRY_RELATIVE,
        f"symmetry before symmetrising: max |d(i,j) - d(j,i)| = {report['max_asymmetry_km']:.3e} km "
        f"({rel:.2e} relative; limit {C.VERIFY_MAX_ASYMMETRY_RELATIVE:g})",
    )
    fin = np.isfinite(D)
    check(np.array_equal(fin, fin.T) and np.array_equal(D[fin], D.T[fin]), "output matrix is exactly symmetric")
    check(float(D[fin].min()) >= 0.0, f"non-negativity: min resolved entry = {float(D[fin].min()):.6f} km")
    check(not np.any(np.diag(D)), f"diagonal: max |d(i,i)| = {float(np.abs(np.diag(D)).max()):.3e} km")

    # Check the *published matrix*, not only the reduced core. A symmetric
    # offset added to every off-diagonal entry preserves non-negativity and
    # triangle inequality, so the structural tests above cannot find it.
    # The core-vs-unreduced comparison below checks contraction accuracy, but
    # without this reconstruction it says nothing about the published NPY.
    if report.get("include_snap_offset") != bool(C.INCLUDE_SNAP_OFFSET):
        raise SystemExit(
            "out/ was computed with a different INCLUDE_SNAP_OFFSET setting. "
            "Rerun: python run.py matrix"
        )
    core_D = np.load(Path(C.WORK) / "core_matrix.npy", mmap_mode="r")
    core_radius = np.load(Path(C.WORK) / "core_radius.npy")
    inv_full = np.zeros(s["ids"].size, dtype=np.int64)
    inv_full[np.flatnonzero(cs["snapped"])] = cs["inv"]
    expected_D, expected_radius, _ = mx.expand_and_finalise(
        core_D, core_radius, inv_full, s["offset"], cs["snapped"],
        include_snap_offset=C.INCLUDE_SNAP_OFFSET, verbose=False,
    )
    check(
        np.array_equal(town_radius, expected_radius),
        "published row-validity radii match the reduced-core checkpoint",
    )
    same_finite = np.array_equal(np.isfinite(D), np.isfinite(expected_D))
    check(same_finite, "published matrix reachability matches the reduced-core checkpoint")
    if same_finite:
        finite = np.isfinite(expected_D)
        error = float(np.max(np.abs(D[finite] - expected_D[finite]))) if finite.any() else 0.0
        check(
            error <= C.VERIFY_TOL_KM,
            f"published matrix matches independently reconstructed distances "
            f"(max difference {error:.3e} km; tol {C.VERIFY_TOL_KM:g} km)",
        )

    # Triangle inequality. Shortest-path distances satisfy it exactly, and
    # adding the non-negative stubs o_i + o_j to every off-diagonal pair keeps
    # it: d(a,b) + d(b,c) pays o_b twice, which only adds slack. Every finite
    # entry is exact even in a partial matrix, so any violation beyond
    # floating-point rounding means a defect.
    rng = np.random.default_rng(C.VERIFY_SEED)
    idx = rng.integers(0, D.shape[0], (200_000, 3))
    a, b, c = idx[:, 0], idx[:, 1], idx[:, 2]
    m = fin[a, b] & fin[b, c] & fin[a, c]
    if m.any():
        slack = D[a[m], c[m]] - (D[a[m], b[m]] + D[b[m], c[m]])
        worst = max(0.0, float(slack.max()))
        check(
            worst <= C.VERIFY_TOL_KM,
            f"triangle inequality: worst violation {worst:.3e} km over {int(m.sum()):,} "
            f"sampled triples (tol {C.VERIFY_TOL_KM:g} km)",
        )

    # The check that matters: recompute some rows on the UNREDUCED graph.
    if C.VERIFY_N_SOURCES:
        lines.append("")
        lines.append(f"re-running {C.VERIFY_N_SOURCES} sources against the UNREDUCED graph")
        g = np.load(Path(C.WORK) / "graph_full.npz")
        n_nodes = g["node_lonlat"].shape[0]
        u, v, w = rd.dedupe_edges(g["u"], g["v"], g["w"], n_nodes)
        src_orig = np.unique(s["node_idx"][cs["snapped"]])
        pick = rng.choice(src_orig.size, min(C.VERIFY_N_SOURCES, src_orig.size), replace=False)

        t0 = time.time()
        ref = dijkstra(
            mx.build_csr(u, v, w, n_nodes), directed=False, indices=src_orig[pick]
        )[:, src_orig]
        full_s = (time.time() - t0) / len(pick)

        core = np.load(Path(C.WORK) / "core_matrix.npy", mmap_mode="r")
        radius = np.load(Path(C.WORK) / "core_radius.npy")
        got = np.asarray(core[pick, :])
        # Only rows that are complete can be compared to an unlimited run.
        rows_ok = np.isinf(radius[pick])
        if not rows_ok.any():
            lines.append("  (no complete rows in the sample yet; skipped)")
        else:
            ref, got = ref[rows_ok], got[rows_ok]
            both = np.isfinite(ref) & np.isfinite(got)
            mism = int((np.isfinite(ref) != np.isfinite(got)).sum())
            err = float(np.max(np.abs(ref[both] - got[both]))) if both.any() else 0.0
            lines.append(f"  complete rows compared: {int(rows_ok.sum())}")
            lines.append(f"  unreduced graph cost {full_s:.2f} s/source")
            check(mism == 0, f"reachability mismatches vs unreduced graph: {mism}")
            check(
                err <= C.VERIFY_TOL_KM,
                f"max absolute difference vs unreduced graph: {err:.3e} km (tol {C.VERIFY_TOL_KM:g})",
            )

    lines.append("")
    lines.append(f"RESULT: {'FAILED' if failures else 'all checks passed'}")
    txt = "\n".join(lines)
    _atomic_write(Path(C.OUT) / "verification.txt", lambda fh: fh.write(txt + "\n"))
    print(txt)
    if failures:
        raise SystemExit("VERIFICATION FAILED:\n  " + "\n  ".join(failures))


STAGES = {"graph": stage_graph, "reduce": stage_reduce, "matrix": stage_matrix, "verify": stage_verify}


def _parse():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=None,
                   help="input directory (or set PH_NET_DISTANCE_DATA); later stages "
                        "default to the directory recorded by `graph`")
    p.add_argument("--work", type=Path, default=None,
                   help="intermediate directory (or set PH_NET_DISTANCE_WORK)")
    p.add_argument("--out", type=Path, default=None,
                   help="output directory (or set PH_NET_DISTANCE_OUT)")
    p.add_argument("stage", nargs="?", default="all", choices=["all", *STAGES])
    p.add_argument("--workers", type=int, default=None, help="worker processes (0 = cores - 1)")
    p.add_argument("--batch", type=int, default=None, help="sources per Dijkstra call")
    p.add_argument("--waves", type=str, default=None,
                   help="comma-separated Euclidean neighbour orders, e.g. '1,2'; '' disables")
    p.add_argument("--wave-scale", type=float, default=None, dest="wave_scale")
    p.add_argument("--no-partial", action="store_true", help="do not rewrite out/ after each wave")
    p.add_argument(
        "--restart-reduce", action="store_true",
        help="discard the stage-2 checkpoint and restart reduction from stage-1 files",
    )
    a = p.parse_args()
    if a.workers is not None and a.workers < 0:
        p.error("--workers must be 0 (automatic) or a positive integer")
    if a.batch is not None and a.batch < 1:
        p.error("--batch must be a positive integer")
    if a.wave_scale is not None and (not np.isfinite(a.wave_scale) or a.wave_scale <= 0):
        p.error("--wave-scale must be a finite positive number")
    if a.data is not None:
        global DATA_EXPLICIT
        DATA_EXPLICIT = True
        C.DATA = a.data.expanduser().resolve()
    if a.work is not None:
        C.WORK = a.work.expanduser().resolve()
    if a.out is not None:
        C.OUT = a.out.expanduser().resolve()
    if a.waves is not None:
        try:
            a.waves = [int(x) for x in a.waves.split(",") if x.strip()]
        except ValueError:
            p.error(f"--waves must be comma-separated positive integers, e.g. '1,2' (got {a.waves!r})")
        if any(x < 1 for x in a.waves):
            p.error("--waves orders must be positive integers")
    return a


if __name__ == "__main__":
    args = _parse()
    if args.stage == "all":
        for fn in STAGES.values():
            fn(args)
    else:
        STAGES[args.stage](args)
