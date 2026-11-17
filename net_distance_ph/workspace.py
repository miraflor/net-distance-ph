"""Workspace, source acquisition, and timestamped run logging for net-distance-ph.

This module orchestrates the existing source builders. It does not alter their
validation or the shortest-path algorithms. Imported only by the CLI.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
import shutil
import tempfile
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
    from .source_names import accepted_source_names
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
                if candidate.name not in accepted_source_names(key):
                    raise ValueError(f"unexpected name for {key}: {candidate.name}")
                for ext in (".shp", ".shx", ".dbf"):
                    if not candidate.with_suffix(ext).is_file():
                        raise ValueError(f"missing {ext}: {candidate}")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Selected source {pointer} is invalid: {exc}. "
                               "Refusing to overwrite it automatically.") from exc
        configured_level = os.environ.get("NET_DISTANCE_PH_BOUNDARY_LEVEL")
        if configured_level and record.get("boundary_level", "town") != configured_level:
            raise RuntimeError(f"Selected boundary level {record.get('boundary_level', 'town')} "
                               f"differs from requested {configured_level}; use a separate workspace")
        if record.get("boundary_level", "town") != "town":
            roster_rel = Path(record.get("roster", ""))
            if (not record.get("roster") or roster_rel.is_absolute() or ".." in roster_rel.parts
                    or not (root / roster_rel).is_file()):
                raise RuntimeError(f"Selected {record['boundary_level']} boundary roster missing or unsafe in {pointer}")
            from .boundaries import sha256
            if record.get("roster_sha256") != sha256(root / roster_rel):
                raise RuntimeError(f"Selected {record['boundary_level']} roster checksum differs from pointer: {pointer}")
        return True

    # The old CLI accepted manually prepared shapefiles under one data root.
    # Respect complete legacy inputs, without considering historical releases
    # for which the selection pointer is missing.
    hits_by_key = {}
    for key, filename in filename_by_key.items():
        hits = [p for allowed in accepted_source_names(key)
                for p in root.rglob(allowed) if not any(
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
    """Automatically validate and publish the pinned PSGC-aligned source.

    Replaces the old default geoBoundaries reconciliation, but never modifies
    an existing selection pointer or continues after a validation error.
    """
    from net_distance_ph.boundaries import run as import_boundaries
    level = os.environ.get("NET_DISTANCE_PH_BOUNDARY_LEVEL", "town")
    status = import_boundaries(workspace=Path(root).parent, download=True,
                               publish=True, level=level)
    if status != 0:
        raise RuntimeError(f"PSGC-aligned {level} boundary preparation failed (exit {status}); "
                           "existing boundary releases were not replaced")


def _selected_transport_profile(root):
    """Inspect published class list (never infer it from a release filename).

    Returns None for a historical source without an auditable manifest; such
    custom/legacy imports are not silently migrated.
    """
    pointer = Path(root) / "current_transport.json"
    if not pointer.is_file():
        return None
    record = json.loads(pointer.read_text(encoding="utf-8"))
    rel = record.get("provenance")
    if not rel:
        return None
    relpath = Path(rel)
    if relpath.is_absolute() or ".." in relpath.parts:
        raise RuntimeError(f"Unsafe transport provenance path: {relpath}")
    manifest_path = (Path(root) / relpath).resolve()
    manifest_path.relative_to(Path(root).resolve())
    if not manifest_path.is_file():
        raise RuntimeError(f"Selected transport release has missing provenance: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    classes = manifest.get("road_filter")
    if not isinstance(classes, list) or not all(isinstance(c, str) for c in classes):
        raise RuntimeError(f"Selected transport release has no auditable road_filter: {manifest_path}")
    return set(classes), record


def _archive_baseline_before_replacement(root, selection):
    """Save a copy of previous stage-1 graph + audit before switching inputs.

    The old immutable transport release is not changed. A completed baseline
    directory is never overwritten on a subsequent attempt.
    """
    root = Path(root)
    workspace = root.parent
    release_id = selection.get("release_id")
    if not isinstance(release_id, str) or not release_id or "/" in release_id or "\\" in release_id or release_id in {".", ".."}:
        raise RuntimeError("Selected transport release has no safe release ID")
    base = workspace / "baselines"
    dest = base / f"transport-{release_id}"
    if dest.exists():
        print(f"REUSE: previous transport baseline archive {dest}")
        return dest
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".baseline-", dir=base) as temp:
        stage = Path(temp) / "payload"
        stage.mkdir()
        (stage / "current_transport.json").write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
        for rel in ("work/graph_full.npz", "work/sources.npz"):
            src = workspace / rel
            if src.is_file():
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        report_dir = workspace / "output" / "connectivity_audit"
        if report_dir.is_dir():
            shutil.copytree(report_dir, stage / "output" / "connectivity_audit")
        # Baseline snapshots are historical artifacts, not validated graph releases.
        (stage / "README.txt").write_text(
            "Prior selected transport pointer and any existing graph/audit snapshots.\n"
            "Saved graph provenance must be independently checked before reuse.\n",
            encoding="utf-8")
        os.replace(stage, dest)
    print(f"ARCHIVED: previous transport pointer, graph and audit at {dest}")
    return dest


def _build_transport(root, *, replace=False, selected=None):
    """Prepare exact desired default using existing national PBF when replacing."""
    from scripts.build_transport import main, ROAD_PROFILES
    profile = os.environ.get("NET_DISTANCE_PH_ROAD_PROFILE", "comprehensive")
    if profile not in ROAD_PROFILES:
        raise ValueError(f"Unknown road profile: {profile}")
    args = ["--output", str(root), "--road-profile", profile]
    if replace:
        # Never fetch a different OSM snapshot while migrating the selected
        # release: exact file bytes must match the old recorded source hash.
        if selected is None or not selected.get("source_sha256"):
            raise RuntimeError("Cannot replace selected transport without recorded OSM source SHA-256")
        local_pbf = Path(root) / "sources" / "philippines-latest.osm.pbf"
        if not local_pbf.is_file():
            raise FileNotFoundError(f"Cached OSM PBF missing: {local_pbf}; refusing replacement/download")
        args += ["--pbf", str(local_pbf), "--expected-sha256", selected["source_sha256"], "--replace"]
    else:
        args += ["--download"]
    status = main(args)
    if status != 0:
        raise RuntimeError(f"Transport source preparation failed (exit {status}); "
                           "the selected transport release was not intentionally changed")


def prepare_sources(root, *, heartbeat_seconds=30):
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(f"Source data root: {root}")
    boundaries_ok = _selected(root, "current_boundaries.json", {"centroids": "muncity_centroids.shp"})
    transport_ok = _selected(root, "current_transport.json", {
        "roads": "primsectertrunk_road.shp", "ferries": "ferry_route_shape.shp"})
    if not boundaries_ok:
        with timed_step("Validate pinned PSGC-aligned boundaries and generate selected origins", heartbeat_seconds):
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
        checked = _selected_transport_profile(root)
        if checked is None:
            print("REUSE: selected legacy transport has no class manifest; retaining user-supplied input")
        else:
            classes, record = checked
            from scripts.build_transport import ROAD_PROFILES
            profile = os.environ.get("NET_DISTANCE_PH_ROAD_PROFILE", "comprehensive")
            if profile not in ROAD_PROFILES:
                raise ValueError(f"Unknown road profile: {profile}")
            expected = set(ROAD_PROFILES[profile])
            if classes != expected:
                print(f"ROAD PROFILE CHANGE: recorded={sorted(classes)}; requested={sorted(expected)}")
                with timed_step("Re-extract requested road profile from cached OSM snapshot", heartbeat_seconds):
                    _archive_baseline_before_replacement(root, record)
                    _build_transport(root, replace=True, selected=record)
                updated = _selected_transport_profile(root)
                if updated is None or updated[0] != expected:
                    raise RuntimeError("Transport rebuild returned without publishing the requested road class profile")
            else:
                print(f"REUSE: validated existing transport profile={profile} (no download/extraction)")
    print("READY: selected boundary and transport inputs pass file-presence checks")
