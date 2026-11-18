"""Installed command-line interface for net-distance-ph.

The existing ``run`` module remains the execution engine. Each stage runs in
its own process so the legacy code keeps its existing source provenance,
checkpoint, signal handling, workspace, and logging semantics. This module
does not modify graph algorithms.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from .workspace import paths, workspace_path, log_prefix

STAGES = ("prepare", "graph", "connectivity", "reduce", "matrix", "verify")
DIRECT_STAGES = STAGES
from .boundary_levels import LEVELS

DATA_DEPENDENCIES = ("numpy", "scipy", "pandas", "geopandas", "pyproj", "osmium", "shapefile")


def _add_shared(parser: argparse.ArgumentParser, *, stage: str | None = None) -> None:
    parser.add_argument("--workspace", type=Path, metavar="PATH", help="workspace root (default: ~/Documents/net-distance-ph)")
    parser.add_argument("--boundary-level", choices=LEVELS, default="town",
                        help="geographic origins: town (default), city, municipality, huc, province, region, barangay")
    if stage == "status":
        parser.add_argument("--data", type=Path, metavar="PATH", help="input root to inspect")
        parser.add_argument("--work", type=Path, metavar="PATH", help="graph folder to inspect")
        parser.add_argument("--out", type=Path, metavar="PATH", help="output folder to inspect")
    if stage not in {"status", "doctor"}:
        parser.add_argument("--data", type=Path, metavar="PATH", help="input root (default: workspace/data)")
        parser.add_argument("--work", type=Path, metavar="PATH", help="graph and checkpoint folder")
        parser.add_argument("--out", type=Path, metavar="PATH", help="output folder")
        parser.add_argument("--verbose", action="store_true", help="print selected source and configuration details")
        parser.add_argument("--heartbeat-seconds", type=float, metavar="SECONDS", help="liveness message interval")
    if stage in {"prepare", "graph", "run"}:
        parser.add_argument("--road-profile", choices=("comprehensive", "major"),
                            default="comprehensive", help="road filter: comprehensive default (14 classes), or historical major (4 classes)")
    if stage in {"graph", "run"}:
        parser.add_argument("--no-auto-prepare", action="store_true", help="do not download or rebuild missing graph inputs")
    if stage in {"matrix", "run"}:
        parser.add_argument("--workers", type=int, metavar="N", help="worker processes (0 = automatic)")
        parser.add_argument("--batch", type=int, metavar="N", help="source batch size")
        parser.add_argument("--waves", metavar="ORDERS", help="neighbor wave orders, e.g. '1,2' or ''")
        parser.add_argument("--wave-scale", type=float, metavar="FACTOR", help="radius multiplier")
        parser.add_argument("--no-partial", action="store_true", help="disable partial matrix publishing")
    if stage in {"reduce", "run"}:
        parser.add_argument("--restart-reduce", action="store_true", help="restart reduction from its stage-1 inputs")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="net-distance-ph",
        description="Build and audit Philippine city/municipality road-and-ferry networks.",
        epilog="Start with 'net-distance-ph status' or 'net-distance-ph doctor'.",
    )
    parser.add_argument("--version", action="version", version=_version())
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    for stage in DIRECT_STAGES:
        purpose = {
            "prepare": "acquire and reconcile national input sources",
            "graph": "build the road/ferry graph and attach localities",
            "connectivity": "audit complete PSGC coverage and modeled reachability",
            "reduce": "contract the graph without changing distances",
            "matrix": "calculate/resume town-to-town distances",
            "verify": "verify the distance matrix against the original graph",
        }[stage]
        p = sub.add_parser(stage, help=purpose, description=purpose)
        _add_shared(p, stage=stage)

    p = sub.add_parser("boundaries", help="validate or publish pinned PSGC-aligned ADM3 polygons", description="Validate a pinned third-party municipality/city polygon source; explicit --publish required")
    p.add_argument("--workspace", type=Path, metavar="PATH", help="workspace root")
    p.add_argument("--boundary-level", "--level", choices=LEVELS, default="town",
                   help="select PSGC polygon level (default: town)")
    boundary_source = p.add_mutually_exclusive_group(required=True)
    boundary_source.add_argument("--source", type=Path, metavar="ZIP", help="local v1.0.1 ADM3 shapefile ZIP")
    boundary_source.add_argument("--download", action="store_true", help="download the pinned 160 MB ZIP into the workspace cache")
    p.add_argument("--publish", action="store_true", help="after validation, publish a complete boundary release")
    p.add_argument("--replace", action="store_true", help="explicitly replace an existing selected boundary release (requires --publish)")

    p = sub.add_parser("diagnose-connectivity", help="inspect nearest graph-vertex gaps without modifying the saved graph")
    p.add_argument("--workspace", type=Path, metavar="PATH", help="workspace root")
    p.add_argument("--boundary-level", choices=LEVELS, default="town", help="level of existing saved graph (default: town)")
    p.add_argument("--top", type=int, default=20, metavar="N", help="how many smallest gaps to print")

    p = sub.add_parser("inspect-osm-gaps", help="read-only examination of cached OSM way/node topology at component gaps")
    p.add_argument("--workspace", type=Path, metavar="PATH", help="workspace root")
    p.add_argument("--boundary-level", choices=LEVELS, default="town", help="geographic level of saved graph (default: town)")
    p.add_argument("--top", type=int, default=9, metavar="N", help="number of closest gap components to examine (default: 9)")
    p.add_argument("--radius-m", type=float, default=1000.0, metavar="METRES", help="local path and way search radius per gap endpoint (default: 1000)")

    p = sub.add_parser("run", help="execute a controlled, stop-on-failure range of stages")
    _add_shared(p, stage="run")
    selectors = p.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--until", choices=STAGES, metavar="STAGE",
                           help="run from prepare through STAGE, inclusive")
    selectors.add_argument("--from", dest="from_stage", choices=STAGES, metavar="STAGE",
                           help="run from STAGE through verify, inclusive")
    selectors.add_argument("--only", choices=STAGES, metavar="STAGE", help="run only STAGE")
    selectors.add_argument("--all", action="store_true", help="run all stages (connectivity is a gate)")

    # Native, preview-first cleanup of computed state and generated artifacts.
    p = sub.add_parser("reset", help="preview or erase generated project state (safe by default)")
    p.add_argument("--workspace", type=Path, help="workspace directory (default: ~/Documents/net-distance-ph)")
    p.add_argument("--all", action="store_true", help="also clean the checkout to its pushed current branch")
    p.add_argument("--purge-patch-artifacts", action="store_true",
                   help="also remove enumerated old patch archives, extracted installers, and backups")
    p.add_argument("--execute", action="store_true", help="perform deletion after typed confirmation; default is preview")
    p.add_argument("--run", action="store_true", help="after --all --execute, build prepare -> graph -> population-weighted")
    p.add_argument("--workers", type=int, default=2, help="routing worker processes for optional fresh run")
    p.add_argument("--heartbeat-seconds", type=float, default=30.0, help="heartbeat interval for fresh run")

    p = sub.add_parser("observe", help="read-only audit of logs and timing; no computation")
    p.add_argument("--workspace", type=Path, help="existing workspace")
    p.add_argument("--json", action="store_true", help="print full report JSON")
    p.add_argument("--no-write", action="store_true", help="do not write observability_report.json")

    p = sub.add_parser("audit-geography", help="audit province exclusions, road/ferry topology, and population reachability")
    p.add_argument("--workspace", type=Path, help="workspace containing existing completed output")
    p.add_argument("--run-dir", type=Path, help="existing routing output directory (default latest)")
    p.add_argument("--province-crosswalk", type=Path, help="original crosswalk if the saved run used one")
    p.add_argument("--top", type=int, default=100, help="number of top zero/partial town pairs (default 100)")
    p.add_argument("--all-pairs", action="store_true", help="stream all noncomplete town pairs to CSV (can be large)")
    p.add_argument("--skip-graph", action="store_true", help="skip memory-intensive stage-1 original graph connectivity/ferry scan")

    p = sub.add_parser("audit-topology", help="trace disconnected population representatives through original and reduced graphs")
    p.add_argument("--workspace", type=Path, help="existing workspace (default ~/Documents/net-distance-ph)")
    p.add_argument("--run-dir", type=Path, help="existing population-weighted run (default latest)")
    p.add_argument("--top-components", type=int, default=15, help="number of largest nonmain populated components to inspect (default 15)")
    p.add_argument("--search-km", type=float, default=10.0, help="local geometric candidate search radius (default 10 km; does NOT add edges)")
    p.add_argument("--lineage-only", action="store_true", help="check original-versus-reduced component identity without searching nearby gaps")

    p = sub.add_parser("inspect-topology-osm", help="read-only OSM node-ID investigation of v50 populated component gaps")
    p.add_argument("--workspace", type=Path, help="existing workspace (default ~/Documents/net-distance-ph)")
    p.add_argument("--top", type=int, default=15, help="top v50 populated components (default 15)")
    p.add_argument("--ranks", nargs="+", type=int, help="specific v50 priority ranks (e.g. 2 6 7 8 9)")
    p.add_argument("--relation", choices=("main", "other", "both"), default="both",
                   help="nearest main, nearest other, or both (default both; identical endpoint pairs deduplicated)")
    p.add_argument("--radius-m", type=float, default=1000.0,
                   help="local OSM way inspection radius in metres (100..10000; default 1000)")

    p = sub.add_parser("audit-origin-snap", help="read-only population origin snapping sensitivity against main graph component")
    p.add_argument("--workspace", type=Path, help="existing workspace (default ~/Documents/net-distance-ph)")
    p.add_argument("--run-dir", type=Path, help="existing population-weighted routing run (default latest)")
    p.add_argument("--points-file", type=Path, help="original point geometry source if no matching cached default is found")
    p.add_argument("--top", type=int, default=15, help="largest nonmain populated components to audit (default 15)")
    p.add_argument("--ranks", nargs="+", type=int, help="specific original v50 priority ranks, e.g. 2 6 7 8 9")

    p = sub.add_parser("audit-polygon-network", help="v53 read-only six-barangay polygon/road network GIS consistency")
    p.add_argument("--workspace", type=Path, help="existing workspace (default ~/Documents/net-distance-ph)")
    p.add_argument("--run-dir", type=Path, help="existing population-weighted routing run (default latest)")

    # Optional population-weighted matrices; baseline run stages remain unchanged.
    p = sub.add_parser("population-weighted", help="optional population-weighted network distance matrices")
    from .population_weighted import add_run_arguments
    add_run_arguments(p)

    p = sub.add_parser("view", help="open the read-only local population-weighted output explorer")
    p.add_argument("--workspace", type=Path, help="existing workspace (default ~/Documents/net-distance-ph)")
    p.add_argument("--port", type=int, default=0, help="local loopback HTTP port (0 = automatic)")
    p.add_argument("--no-open", action="store_true", help="print URL without opening the browser")

    p = sub.add_parser("status", help="show which inputs, artifacts and reports exist (no computation)")
    _add_shared(p, stage="status")
    p.add_argument("--json", action="store_true", help="print machine-readable status")

    p = sub.add_parser("doctor", help="check Python dependencies and input shapefiles (no downloads)")
    _add_shared(p, stage="doctor")
    p.add_argument("--data", type=Path, metavar="PATH", help="input root (default: workspace/data)")
    return parser


def _version() -> str:
    try:
        return importlib.metadata.version("net-distance-ph")
    except importlib.metadata.PackageNotFoundError:
        return "development checkout"


def _effective_workspace(args: argparse.Namespace) -> Path:
    root = workspace_path(args.workspace)
    level = getattr(args, "boundary_level", "town")
    return root if level == "town" else root / "levels" / level


def _base_args(args: argparse.Namespace) -> list[str]:
    level = getattr(args, "boundary_level", "town")
    if level != "town" and (any(getattr(args, x, None) is not None for x in ("data", "work", "out"))
            or any(name in os.environ for name in ("NET_DISTANCE_PH_DATA", "NET_DISTANCE_PH_WORK", "NET_DISTANCE_PH_OUT"))):
        raise ValueError("Use --workspace, not explicit --data/--work/--out or their environment "
                         "variables, for non-town levels; this prevents mixing graph artifacts")
    workspace = _effective_workspace(args)
    result = ["--workspace", str(workspace)]
    for name in ("data", "work", "out"):
        value = getattr(args, name, None)
        if value is not None:
            result.extend([f"--{name}", str(value.expanduser().resolve())])
    if getattr(args, "verbose", False):
        result.append("--verbose")
    if args.heartbeat_seconds is not None:
        if not 0 < args.heartbeat_seconds < float("inf"):
            raise ValueError("--heartbeat-seconds must be a finite positive number")
        result.extend(["--heartbeat-seconds", str(args.heartbeat_seconds)])
    return result


def _stage_args(stage: str, args: argparse.Namespace) -> list[str]:
    argv = [stage, *_base_args(args)]
    if stage == "graph" and getattr(args, "no_auto_prepare", False):
        argv.append("--no-auto-prepare")
    if stage == "reduce" and getattr(args, "restart_reduce", False):
        argv.append("--restart-reduce")
    if stage == "matrix":
        for name in ("workers", "batch", "waves", "wave_scale"):
            value = getattr(args, name, None)
            if value is not None:
                argv.extend(["--" + name.replace("_", "-"), str(value)])
        if getattr(args, "no_partial", False):
            argv.append("--no-partial")
    return argv


def _invoke_stage(stage: str, args: argparse.Namespace) -> int:
    # run.py is installed as the Python module ``run`` (and remains directly
    # invocable as a legacy script from the checkout). Running it in a fresh
    # process avoids leaking the mutable global config into the next stage.
    command = [sys.executable, "-m", "run", *_stage_args(stage, args)]
    print(log_prefix() + " >>> net-distance-ph: " + stage, flush=True)
    try:
        level = getattr(args, "boundary_level", "town")
        env = dict(os.environ)
        env["NET_DISTANCE_PH_BOUNDARY_LEVEL"] = level
        if stage in {"prepare", "graph"}:
            env["NET_DISTANCE_PH_ROAD_PROFILE"] = getattr(args, "road_profile", "comprehensive")
        return subprocess.run(command, check=False, env=env).returncode
    except FileNotFoundError as exc:
        print(f"ERROR: Cannot launch Python: {exc}", file=sys.stderr)
        return 2


def _selection(args: argparse.Namespace) -> tuple[str, ...]:
    if args.command != "run":
        return (args.command,)
    if args.only is not None:
        return (args.only,)
    if args.until is not None:
        return STAGES[: STAGES.index(args.until) + 1]
    if args.from_stage is not None:
        return STAGES[STAGES.index(args.from_stage):]
    if args.all:
        return STAGES
    raise AssertionError("argparse should require a stage selector")


def _perform(args: argparse.Namespace) -> int:
    stages = _selection(args)
    print(log_prefix() + " Stage plan: " + " -> ".join(stages), flush=True)
    if getattr(args, "boundary_level", "town") != "town":
        print(log_prefix() + f" Boundary level: {args.boundary_level}; isolated workspace: {_effective_workspace(args)}", flush=True)
    if "connectivity" in stages:
        print(log_prefix() + " Connectivity is a gate: disconnected or missing towns stop this run.", flush=True)
    for stage in stages:
        exit_code = _invoke_stage(stage, args)
        if exit_code != 0:
            print(log_prefix() + f" STOPPED at {stage} (exit {exit_code}). Later stages were not run.", file=sys.stderr)
            if stage == "prepare" or stage == "graph":
                print(log_prefix() + " Inspect boundary reconciliation and source-preparation logs before proceeding.", file=sys.stderr)
            return exit_code
    print(log_prefix() + " ALL REQUESTED STAGES COMPLETED. Review model limitations and verification outputs.", flush=True)
    return 0


def _paths_for(args: argparse.Namespace) -> dict[str, Path]:
    result = paths(_effective_workspace(args))
    for flag, name in (("data", "data"), ("work", "work"), ("out", "output")):
        override = getattr(args, flag, None)
        if override is not None:
            result[name] = override.expanduser().resolve()
    return result


def _status(args: argparse.Namespace) -> int:
    w = _paths_for(args)
    entries = {
        "boundaries_selected": w["data"] / "current_boundaries.json",
        "transport_selected": w["data"] / "current_transport.json",
        "graph": w["work"] / "graph_full.npz",
        "locality_attachments": w["work"] / "sources.npz",
        "reduced_graph": w["work"] / "core_sources.npz",
        "connectivity_report": w["output"] / "connectivity_audit" / "connectivity_report.json",
        "matrix_report": w["output"] / "matrix_report.json",
        "verification": w["output"] / "verification.txt",
    }
    state = {
        "workspace": str(w["workspace"]),
        "data": str(w["data"]),
        "work": str(w["work"]),
        "output": str(w["output"]),
        "files": {name: {"exists": path.is_file(), "path": str(path)} for name, path in entries.items()},
        "warning": "Presence of artifacts is not evidence that data are current or routes physically usable.",
    }
    if args.json:
        print(json.dumps(state, indent=2))
    else:
        print(f"Workspace: {w['workspace']}")
        for name, entry in state["files"].items():
            print(f"  {'FOUND  ' if entry['exists'] else 'MISSING'} {name:<24} {entry['path']}")
        print("\nStatus reports file presence, not verified research results.")
        if not state["files"]["graph"]["exists"] or not state["files"]["locality_attachments"]["exists"]:
            print("Next: net-distance-ph prepare   (if inputs are missing)")
            print("Then: net-distance-ph graph")
            print("Then: net-distance-ph connectivity")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    w = _paths_for(args)
    data = args.data.expanduser().resolve() if args.data else w["data"]
    print(f"Python: {sys.executable}\nWorkspace: {w['workspace']}\nSource data: {data}", flush=True)
    available = True
    for package in DATA_DEPENDENCIES:
        found = importlib.util.find_spec(package) is not None
        print(f"  {'OK' if found else 'MISSING'} dependency {package}")
        available &= found
    if not available:
        print('Install the GIS extras: python -m pip install -e ".[geo,transport,test]"', file=sys.stderr)
    print("\nInput shapefile check (no network or downloads):", flush=True)
    try:
        code = subprocess.run([sys.executable, "-m", "check_setup", "--data", str(data)], check=False).returncode
    except FileNotFoundError:
        code = 2
    if code:
        print("Source files are incomplete or invalid. Use 'net-distance-ph prepare' to prepare them.", file=sys.stderr)
    if available and code == 0:
        print("DOCTOR PASS: dependencies and input file headers detected. Geographic validity still requires auditing.")
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "view":
        from .view import run_view
        try:
            return run_view(args.workspace, port=args.port, open_browser=not args.no_open)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"VIEW ERROR: {exc}", file=sys.stderr)
            return 2
    if args.command == "reset":
        from .reset import run as reset_run
        try:
            return reset_run(args)
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"RESET REFUSED: {exc}", file=sys.stderr)
            return 2
    if args.command == "audit-polygon-network":
        from .polygon_network_audit import execute as polygon_network_execute
        from .workspace import run_log
        try:
            with run_log(workspace_path(args.workspace)):
                return polygon_network_execute(args)
        except (OSError, ValueError, RuntimeError, KeyError, ImportError) as exc:
            print(log_prefix() + f" POLYGON NETWORK AUDIT REFUSED: {exc}", file=sys.stderr)
            return 2
    if args.command == "audit-origin-snap":
        from .origin_snap_audit import execute as origin_snap_execute
        from .workspace import run_log
        try:
            with run_log(workspace_path(args.workspace)):
                return origin_snap_execute(args)
        except (OSError, ValueError, RuntimeError, KeyError, ImportError) as exc:
            print(log_prefix() + f" ORIGIN SNAP AUDIT REFUSED: {exc}", file=sys.stderr)
            return 2
    if args.command == "inspect-topology-osm":
        from .topology_osm_investigation import execute as osm_investigation
        from .workspace import run_log
        try:
            with run_log(workspace_path(args.workspace)):
                return osm_investigation(args)
        except (OSError, ValueError, RuntimeError, KeyError, ImportError) as exc:
            print(log_prefix() + f" OSM INVESTIGATION REFUSED: {exc}", file=sys.stderr)
            return 2
    if args.command == "audit-topology":
        from .topology_diagnostics import execute as topology_diagnostics
        from .workspace import run_log
        try:
            if not 1 <= args.top_components <= 229:
                raise ValueError("--top-components must be between 1 and 229")
            if not 0 < args.search_km <= 1000:
                raise ValueError("--search-km must be in (0, 1000]")
            with run_log(workspace_path(args.workspace)):
                return topology_diagnostics(args)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(log_prefix() + f" TOPOLOGY AUDIT REFUSED: {exc}", file=sys.stderr)
            return 2
    if args.command == "audit-geography":
        from .geographic_audit import execute as geographic_audit
        from .workspace import run_log
        try:
            if args.top < 1 or args.top > 100000:
                raise ValueError("--top must be between 1 and 100000")
            with run_log(workspace_path(args.workspace)):
                return geographic_audit(args)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(log_prefix() + f" GEOGRAPHIC AUDIT REFUSED: {exc}", file=sys.stderr)
            return 2
    if args.command == "observe":
        from .observability import report_existing
        try:
            report = report_existing(workspace_path(args.workspace), write=not args.no_write)
        except (OSError, ValueError) as exc:
            print(f"OBSERVE ERROR: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(log_prefix() + " Observability report: " + report.get("report_path", "read-only"))
            for name, check in report["checks"].items():
                print(f"  {name}: {check['status']}")
            print("  Scientific validation: not established (evidence inventory only)")
        return 0
    if args.command == "status":
        return _status(args)
    if args.command == "doctor":
        return _doctor(args)
    try:
        if args.command == "population-weighted":
            from .population_weighted import run_options
            return run_options(args)
        if args.command == "inspect-osm-gaps":
            from .osm_gap_inspection import run as run_osm_inspection
            return run_osm_inspection(workspace=_effective_workspace(args),
                                      top=args.top, radius_m=args.radius_m)
        if args.command == "diagnose-connectivity":
            from .connectivity_diagnostics import run as run_diagnostics
            return run_diagnostics(workspace=_effective_workspace(args), top=args.top)
        if args.command == "boundaries":
            from .boundaries import run as run_boundaries
            return run_boundaries(workspace=_effective_workspace(args), source=args.source,
                                  download=args.download, publish=args.publish, replace=args.replace,
                                  level=args.boundary_level)
        return _perform(args)
    except (ValueError, OSError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
