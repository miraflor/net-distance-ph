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


# Unified runtime observability (v47). This is shared by every pipeline stage.
from contextvars import ContextVar as _ContextVar
from datetime import timezone as _timezone

_ACTIVE_TRACE = _ContextVar("net_distance_ph_active_trace", default=None)
_RUN_ORIGIN = _ContextVar("net_distance_ph_run_origin", default=None)
_DEFAULT_ORIGIN = datetime.now(_timezone.utc)


def format_elapsed(seconds):
    """Monotonic elapsed-time display, rounded down to whole seconds."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _iso_now():
    # Local wall clock for human-readable records, intentionally no UTC offset.
    return datetime.now().isoformat(timespec="seconds")


def log_prefix(origin=None):
    """One formatter for session logs and secondary progress logs."""
    if origin is None:
        origin = _RUN_ORIGIN.get()
        if origin is None:
            inherited = os.environ.get("NET_DISTANCE_PH_PIPELINE_STARTED_UTC")
            origin = (datetime.fromisoformat(inherited).astimezone(_timezone.utc)
                      if inherited else _DEFAULT_ORIGIN)
    elapsed = (datetime.now(_timezone.utc) - origin).total_seconds()
    return f"[{datetime.now():%H:%M:%S} e={format_elapsed(elapsed)}]"


class _RunTrace:
    """A small append-only event journal, independent of the numeric pipeline."""

    def __init__(self, workspace, logfile):
        self.lock = threading.RLock()
        self.stack = []
        self.sequence = 0
        self.session_id = logfile.stem
        external = os.environ.get("NET_DISTANCE_PH_JOURNAL_PATH")
        if external:
            journal = Path(external).expanduser().resolve()
            root = (Path(workspace).resolve() / "logs")
            if journal.parent != root:
                raise ValueError("Run journal must reside within workspace/logs")
            self.journal_path = journal
        else:
            self.journal_path = logfile.with_suffix(".jsonl")

    def record(self, event, **fields):
        payload = {"event": event, "at": _iso_now(), "session_id": self.session_id, **fields}
        with self.lock:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")

    def enter(self, label):
        with self.lock:
            self.sequence += 1
            ident = self.sequence
            parent = self.stack[-1][0] if self.stack else None
            self.stack.append((ident, label))
        self.record("phase_start", phase_id=ident, parent_id=parent, phase=label)
        return ident, parent

    def leave(self, ident):
        with self.lock:
            if not self.stack or self.stack[-1][0] != ident:
                raise RuntimeError("Timing phases must be exited in nested order")
            self.stack.pop()

    def current(self, ident):
        with self.lock:
            return bool(self.stack and self.stack[-1][0] == ident)


class _TimestampStream:
    """Compact timezone-free local clock and cumulative elapsed time."""

    def __init__(self, stream, logfile, lock, label, origin=None):
        self._stream = stream
        self._logfile = logfile
        self._lock = lock
        self._label = label
        self._start = True
        self._origin = origin or datetime.now(_timezone.utc)

    def write(self, value):
        if not isinstance(value, str):
            value = str(value)
        if not value:
            return 0
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        with self._lock:
            for chunk in normalized.splitlines(keepends=True):
                if self._start:
                    prefix = log_prefix(self._origin) + " "
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
    filename = datetime.now().astimezone().strftime("run-%Y%m%d-%H%M%S-%f")
    log_path = directory / f"{filename}-{os.getpid()}.log"
    source = os.environ.get("NET_DISTANCE_PH_PIPELINE_STARTED_UTC")
    try:
        origin = datetime.fromisoformat(source).astimezone(_timezone.utc) if source else datetime.now(_timezone.utc)
    except ValueError as exc:
        raise ValueError("Invalid NET_DISTANCE_PH_PIPELINE_STARTED_UTC") from exc
    stdout, stderr = sys.stdout, sys.stderr
    lock = threading.RLock()
    trace = _RunTrace(workspace, log_path)
    with log_path.open("x", encoding="utf-8", buffering=1) as logfile:
        out = _TimestampStream(stdout, logfile, lock, "INFO", origin)
        err = _TimestampStream(stderr, logfile, lock, "ERROR", origin)
        sys.stdout, sys.stderr = out, err
        token = _ACTIVE_TRACE.set(trace)
        clock_token = _RUN_ORIGIN.set(origin)
        try:
            print(f"Run log: {log_path}")
            print(f"Event journal: {trace.journal_path}")
            yield log_path
        finally:
            _ACTIVE_TRACE.reset(token)
            _RUN_ORIGIN.reset(clock_token)
            out.flush()
            err.flush()
            sys.stdout, sys.stderr = stdout, stderr


@contextmanager
def timed_step(name, heartbeat_seconds=30):
    """One heartbeat for the deepest active phase; journal start and finish."""
    started = time.monotonic()
    trace = _ACTIVE_TRACE.get()
    phase_id, _ = trace.enter(name) if trace is not None else (None, None)
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(heartbeat_seconds):
            if trace is None or trace.current(phase_id):
                print(f"STILL RUNNING: {name} ({format_elapsed(time.monotonic() - started)})", flush=True)

    thread = threading.Thread(target=heartbeat, daemon=True, name="net-distance-ph-heartbeat")
    print(f"START: {name}", flush=True)
    thread.start()
    try:
        yield
    except BaseException as exc:
        duration = time.monotonic() - started
        print(f"FAILED: {name} ({format_elapsed(duration)}); "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if trace is not None:
            trace.record("phase_failed", phase=name, phase_id=phase_id,
                         elapsed_seconds=round(duration, 3), error_type=type(exc).__name__, error=str(exc))
        raise
    else:
        duration = time.monotonic() - started
        print(f"DONE: {name} ({format_elapsed(duration)})", flush=True)
        if trace is not None:
            trace.record("phase_finish", phase=name, phase_id=phase_id,
                         elapsed_seconds=round(duration, 3), status="complete")
    finally:
        stop.set()
        thread.join(timeout=1)
        if trace is not None:
            trace.leave(phase_id)


def progress_event(name, completed, total, started, *, unit="items"):
    """Measured processed-source throughput, simple observed-rate ETA, JSONL event."""
    if total <= 0 or completed < 0 or completed > total:
        raise ValueError("Invalid progress counters")
    elapsed = max(0.0, time.monotonic() - started)
    speed = completed / elapsed if elapsed > 0 else 0.0
    eta = (total - completed) / speed if speed > 0 else None
    eta_text = format_elapsed(eta) if eta is not None else "unknown"
    print(f"{name} {completed:,}/{total:,} ({completed/total:.0%}) | "
          f"{speed:.1f}/s | ETA {eta_text}", flush=True)
    trace = _ACTIVE_TRACE.get()
    if trace is not None:
        trace.record("progress", phase=name, completed=int(completed), total=int(total),
                     unit=unit, elapsed_seconds=round(elapsed, 3),
                     rate_per_second=round(speed, 4),
                     eta_seconds=round(eta, 3) if eta is not None else None)


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
