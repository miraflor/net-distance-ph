"""Workspace, source acquisition, and timestamped run logging for net-distance-ph.

This module orchestrates the existing source builders. It does not alter their
validation or the shortest-path algorithms. Imported only by the CLI.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading
import time


def workspace_path(explicit=None):
    raw = explicit if explicit is not None else os.environ.get("NET_DISTANCE_PH_WORKSPACE")
    if raw is not None:
        return Path(raw).expanduser().resolve()
    return (Path.home() / "Documents" / "net-distance-ph").resolve()


def paths(workspace):
    workspace = Path(workspace)
    return {"workspace": workspace, "data": workspace / "data", "work": workspace / "work",
            "output": workspace / "output", "logs": workspace / "logs"}


class _TimestampStream:
    """Prefix each output line and tee it to the run log (thread-safe)."""

    def __init__(self, stream, logfile, lock, label):
        self._stream = stream
        self._logfile = logfile
        self._lock = lock
        self._label = label
        self._start = True

    def write(self, value):
        if not isinstance(value, str):
            value = str(value)
        if not value:
            return 0
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        with self._lock:
            for chunk in normalized.splitlines(keepends=True):
                if self._start:
                    prefix = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] [{self._label}] "
                    self._stream.write(prefix)
                    self._logfile.write(prefix)
                self._stream.write(chunk)
                self._logfile.write(chunk)
                self._start = chunk.endswith("\n")
            self._stream.flush()
            self._logfile.flush()
        return len(value)

    def flush(self):
        with self._lock:
            self._stream.flush()
            self._logfile.flush()

    def isatty(self):
        return self._stream.isatty()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    def fileno(self):
        return self._stream.fileno()


@contextmanager
def run_log(workspace):
    directory = Path(workspace) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().astimezone().strftime("run-%Y%m%d-%H%M%S")
    log_path = directory / f"{filename}-{os.getpid()}.log"
    stdout, stderr = sys.stdout, sys.stderr
    lock = threading.RLock()
    with log_path.open("x", encoding="utf-8", buffering=1) as logfile:
        to_out = _TimestampStream(stdout, logfile, lock, "INFO")
        to_err = _TimestampStream(stderr, logfile, lock, "ERROR")
        sys.stdout, sys.stderr = to_out, to_err
        try:
            print(f"Run log: {log_path}")
            yield log_path
        finally:
            to_out.flush()
            to_err.flush()
            sys.stdout, sys.stderr = stdout, stderr


@contextmanager
def timed_step(name, heartbeat_seconds=30):
    """Report stage start/end, and liveness without claiming numerical progress."""
    started = time.monotonic()
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(heartbeat_seconds):
            print(f"STILL RUNNING: {name}; elapsed {time.monotonic() - started:.1f}s", flush=True)
    thread = threading.Thread(target=heartbeat, daemon=True, name="net-distance-ph-heartbeat")
    print(f"START: {name}", flush=True)
    thread.start()
    try:
        yield
    except BaseException as exc:
        print(f"FAILED: {name}; elapsed {time.monotonic() - started:.1f}s; "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
    else:
        print(f"DONE: {name}; elapsed {time.monotonic() - started:.1f}s", flush=True)
    finally:
        stop.set()
        thread.join(timeout=1)


def _selected(root, pointer_name, filename_by_key):
    """Check the selected release or *only* standalone legacy input files.

    Never silently pick an orphaned release, assume an incomplete set is valid,
    or overwrite an invalid release pointer.
    """
    root = Path(root)
    pointer = root / pointer_name
    if pointer.exists():
        try:
            record = json.loads(pointer.read_text(encoding="utf-8"))
            for key, filename in filename_by_key.items():
                rel = Path(record[key])
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"unsafe relative path for {key}")
                candidate = (root / rel).resolve()
                candidate.relative_to(root.resolve())
                if candidate.name != filename:
                    raise ValueError(f"unexpected name for {key}: {candidate.name}")
                for ext in (".shp", ".shx", ".dbf"):
                    if not candidate.with_suffix(ext).is_file():
                        raise ValueError(f"missing {ext}: {candidate}")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Selected source {pointer} is invalid: {exc}. "
                               "Refusing to overwrite it automatically.") from exc
        return True

    # The old CLI accepted manually prepared shapefiles under one data root.
    # Respect complete legacy inputs, without considering historical releases
    # for which the selection pointer is missing.
    hits_by_key = {}
    for key, filename in filename_by_key.items():
        hits = [p for p in root.rglob(filename) if not any(
            piece in ("boundary_releases", "transport_releases", "boundary_build_incomplete")
            for piece in p.relative_to(root).parts)] if root.exists() else []
        hits_by_key[key] = hits
    if not any(hits_by_key.values()):
        if pointer_name == "current_transport.json" and (root / "transport_releases").exists():
            raise RuntimeError("Transport releases exist without current_transport.json. "
                               "Restore the selection pointer; refusing an arbitrary release.")
        if pointer_name == "current_boundaries.json" and (root / "boundary_releases").exists():
            raise RuntimeError("Boundary releases exist without current_boundaries.json. "
                               "Restore the selection pointer; refusing an arbitrary release.")
        return False
    for key, hits in hits_by_key.items():
        # The original routing engine reads geometries directly from .shp.
        # Road/ferry .dbf attributes are unused, and .shx is optional: the
        # shapefile reader can scan records without an index. Only municipal
        # origins require a DBF because it contains the locality IDs/names.
        if len(hits) != 1 or (key == "centroids" and
                             not hits[0].with_suffix(".dbf").is_file()):
            raise RuntimeError(f"Partial or ambiguous legacy {key} files under {root}. "
                               "Resolve them before automatic acquisition.")
    return True


def _build_boundaries(root):
    from scripts.build_boundaries import main
    status = main(["--output", str(root)])
    if status != 0:
        raise RuntimeError(f"Boundary source preparation failed (exit {status}). "
                           f"Review {root / 'boundary_build_incomplete' / 'boundary_match_audit.csv'}; "
                           "PSA/geoBoundaries vintages may require reviewed overrides.")


def _build_transport(root):
    from scripts.build_transport import main
    status = main(["--download", "--output", str(root)])
    if status != 0:
        raise RuntimeError(f"Transport source preparation failed (exit {status}). "
                           "Check the source download and extraction messages above.")


def prepare_sources(root, *, heartbeat_seconds=30):
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(f"Source data root: {root}")
    boundaries_ok = _selected(root, "current_boundaries.json", {"centroids": "muncity_centroids.shp"})
    transport_ok = _selected(root, "current_transport.json", {
        "roads": "primsectertrunk_road.shp", "ferries": "ferry_route_shape.shp"})
    if not boundaries_ok:
        with timed_step("Acquire PSA + geoBoundaries; reconcile municipalities and generate centroids", heartbeat_seconds):
            _build_boundaries(root)
        if not _selected(root, "current_boundaries.json", {"centroids": "muncity_centroids.shp"}):
            raise RuntimeError("Boundary builder returned without publishing required centroid inputs")
    else:
        print("REUSE: validated existing boundary inputs (no download)")
    if not transport_ok:
        with timed_step("Download/cache OSM Philippines; extract road and ferry layers", heartbeat_seconds):
            _build_transport(root)
        if not _selected(root, "current_transport.json", {
            "roads": "primsectertrunk_road.shp", "ferries": "ferry_route_shape.shp"}):
            raise RuntimeError("Transport builder returned without publishing required network inputs")
    else:
        print("REUSE: validated existing transport inputs (no download)")
    print("READY: selected boundary and transport inputs pass file-presence checks")
