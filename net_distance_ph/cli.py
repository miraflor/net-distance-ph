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
from pathlib import Path
import subprocess
import sys

from .workspace import paths, workspace_path

STAGES = ("prepare", "graph", "connectivity", "reduce", "matrix", "verify")
DIRECT_STAGES = STAGES
DATA_DEPENDENCIES = ("numpy", "scipy", "pandas", "geopandas", "pyproj", "osmium", "shapefile")


def _add_shared(parser: argparse.ArgumentParser, *, stage: str | None = None) -> None:
    parser.add_argument("--workspace", type=Path, metavar="PATH", help="workspace root (default: ~/Documents/net-distance-ph)")
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

    p = sub.add_parser("run", help="execute a controlled, stop-on-failure range of stages")
    _add_shared(p, stage="run")
    selectors = p.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--until", choices=STAGES, metavar="STAGE",
                           help="run from prepare through STAGE, inclusive")
    selectors.add_argument("--from", dest="from_stage", choices=STAGES, metavar="STAGE",
                           help="run from STAGE through verify, inclusive")
    selectors.add_argument("--only", choices=STAGES, metavar="STAGE", help="run only STAGE")
    selectors.add_argument("--all", action="store_true", help="run all stages (connectivity is a gate)")

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


def _base_args(args: argparse.Namespace) -> list[str]:
    workspace = workspace_path(args.workspace)
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
    print("\n>>> net-distance-ph: " + stage, flush=True)
    try:
        return subprocess.run(command, check=False).returncode
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
    print("Stage plan: " + " -> ".join(stages), flush=True)
    if "connectivity" in stages:
        print("Connectivity is a gate: disconnected or missing towns stop this run.", flush=True)
    for stage in stages:
        exit_code = _invoke_stage(stage, args)
        if exit_code != 0:
            print(f"STOPPED at {stage} (exit {exit_code}). Later stages were not run.", file=sys.stderr)
            if stage == "prepare" or stage == "graph":
                print("Inspect boundary reconciliation and source-preparation logs before proceeding.", file=sys.stderr)
            return exit_code
    print("ALL REQUESTED STAGES COMPLETED. Review model limitations and verification outputs.", flush=True)
    return 0


def _paths_for(args: argparse.Namespace) -> dict[str, Path]:
    result = paths(workspace_path(args.workspace))
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
    if args.command == "status":
        return _status(args)
    if args.command == "doctor":
        return _doctor(args)
    try:
        return _perform(args)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
