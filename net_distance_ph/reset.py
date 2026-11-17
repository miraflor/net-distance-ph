"""Careful, preview-first reset of the net-distance-ph *generated* state.

Repository source is NEVER deleted. In --all mode require a clean checkout whose
HEAD equals its fetched upstream, then return to that precise commit and remove
untracked/ignored files. Workspace and selected historical patch artifacts are
removed only after a typed confirmation. Optional --run builds from zero.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

CONFIRM = "RESET NET-DISTANCE-PH TO ZERO"
# Only names of bundles/installers created in these conversations; no globbing.
PATCH_DOWNLOAD_NAMES = (
    "net_distance_ph_adaptive_k_medians_v43.zip",
    "net_distance_ph_adaptive_k_medians_v43",
    "net_distance_ph_timing_v44.zip",
    "net_distance_ph_timing_v44",
    "net_distance_ph_timing_v44_1.zip",
    "net_distance_ph_timing_v44_1",
    "net_distance_ph_fresh_v45.zip",
    "net_distance_ph_fresh_v45",
    "net_distance_ph_fresh_v45_1.zip",
    "net_distance_ph_fresh_v45_1",
    "net-distance-ph-pre-v45-review.zip",
    "net-distance-ph-pre-v45-review",
    "net_distance_ph_reset_v46.zip",
    "net_distance_ph_reset_v46",
)
PATCH_BACKUP_NAMES = (
    "net-distance-ph-adaptive-median-backup",
    "net-distance-ph-timing-backup",
    "net-distance-ph-reset-backup",
)


def _say(message):
    print(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S%z}] {message}", flush=True)


def _git(repo, *args, check=True):
    completed = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", check=False)
    if check and completed.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _guard_path(path, repo, *, kind):
    """Refuse deletion of home, ancestors, repo, nested repo, or links/junctions."""
    original = Path(path).expanduser().absolute()
    if original.is_symlink() or (hasattr(original, "is_junction") and original.is_junction()):
        raise ValueError(f"Refusing to delete linked path: {original}")
    path = original.resolve()
    repo = Path(repo).resolve()
    home = Path.home().resolve()
    if path == repo or path in repo.parents or repo in path.parents:
        raise ValueError(f"{kind} overlaps the Git repository: {path}")
    if path == home or path in home.parents:
        raise ValueError(f"{kind} is home or an ancestor: {path}")
    if path == home / "Documents" or path == home / "Downloads":
        raise ValueError(f"{kind} is a user document root: {path}")
    if path.name != "net-distance-ph" and kind == "Workspace":
        raise ValueError(f"Workspace must be a dedicated directory named 'net-distance-ph': {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{kind} exists but is not a directory: {path}")
    return path


def _assert_repo(repo):
    repo = Path(repo).resolve()
    if repo.name != "net-distance-ph":
        raise ValueError(f"Not a net-distance-ph checkout: {repo}")
    if not (repo / "net_distance_ph" / "cli.py").is_file():
        raise ValueError(f"CLI source not found in: {repo}")
    toplevel = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    if toplevel != repo:
        raise ValueError("Repository path differs from Git root")
    if not _git(repo, "branch", "--show-current"):
        raise ValueError("Detached Git HEAD is unsupported")
    return repo


def _assert_pushed_clean(repo, *, fetch=False):
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise RuntimeError("Tracked or untracked changes exist. Commit and push (or move them) before --all reset.\n" + status[:1500])
    branch = _git(repo, "branch", "--show-current")
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream != "origin/" + branch:
        raise RuntimeError(f"Expected current branch to track origin/{branch}; got {upstream}")
    if fetch:
        _say(f"Fetching current branch from origin: {branch}")
        subprocess.run(["git", "fetch", "origin", branch], cwd=repo, check=True)
    head = _git(repo, "rev-parse", "HEAD")
    remote = _git(repo, "rev-parse", f"refs/remotes/origin/{branch}")
    if head != remote:
        raise RuntimeError(f"Local HEAD {head[:12]} is not equal to origin/{branch} {remote[:12]}; refusing reset")
    return branch, head


def _patch_paths(repo, home):
    d = home / "Downloads"
    return [(d / n).absolute() for n in PATCH_DOWNLOAD_NAMES] + [(repo.parent / n).absolute() for n in PATCH_BACKUP_NAMES]


def _safe_patch_path(path, repo, home):
    path = Path(path).absolute()
    allowed = ((home / "Downloads").resolve(), repo.parent.resolve())
    if path.parent.resolve() not in allowed:
        raise ValueError(f"Patch artifact is outside permitted parents: {path}")
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError(f"Refusing linked patch artifact: {path}")
    return path


def _delete_path(path):
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _run_from_zero(repo, workspace, *, branch, commit, workers, heartbeat):
    logs = workspace / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    journal = logs / "fresh_run_journal.jsonl"
    summary = logs / "fresh_run_summary.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("NET_DISTANCE_PH_")}
    stages = [
        ("prepare", [sys.executable, "-m", "net_distance_ph.cli", "prepare",
                     "--workspace", str(workspace), "--heartbeat-seconds", str(heartbeat), "--verbose"]),
        ("graph", [sys.executable, "-m", "net_distance_ph.cli", "graph",
                   "--workspace", str(workspace), "--no-auto-prepare",
                   "--heartbeat-seconds", str(heartbeat), "--verbose"]),
        ("population-weighted", [sys.executable, "-m", "net_distance_ph.cli", "population-weighted",
                                 "--workspace", str(workspace), "--geometry-source", "faeldon",
                                 "--median-selection", "balanced", "--workers", str(workers),
                                 "--heartbeat-seconds", str(heartbeat)]),
    ]
    started_total = time.monotonic()
    results = []
    for name, argv in stages:
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "start", "stage": name, "at": started, "argv": argv}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _say(f"START: {name}")
        t = time.monotonic()
        try:
            code = subprocess.run(argv, cwd=repo, env=env, check=False).returncode
        except KeyboardInterrupt:
            code = 130
        elapsed = round(time.monotonic() - t, 3)
        result = {"stage": name, "exit_code": code, "elapsed_seconds": elapsed,
                  "started_at": started, "finished_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        results.append(result)
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "finish", **result}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _say(f"{'DONE' if code == 0 else 'FAILED'}: {name}; elapsed {elapsed:.1f}s; exit {code}")
        if code:
            break
    complete = len(results) == len(stages) and all(r["exit_code"] == 0 for r in results)
    summary.write_text(json.dumps({"schema": "net-distance-ph/fresh-run/v46",
       "status": "complete" if complete else "failed", "git_branch": branch, "git_commit": commit,
       "workspace": str(workspace), "total_elapsed_seconds": round(time.monotonic()-started_total, 3),
       "stages": results, "journal": str(journal)}, indent=2) + "\n", encoding="utf-8")
    _say(f"TIMING SUMMARY: {summary}")
    _say(f"EVENT JOURNAL: {journal}")
    return 0 if complete else (results[-1]["exit_code"] or 2)


def run(args, *, repo_override=None, home_override=None):
    """Callable from CLI. Overrides exist only for isolated filesystem tests."""
    repo = _assert_repo(repo_override or Path(__file__).resolve().parents[1])
    home = Path(home_override or Path.home()).resolve()
    default_workspace = home / "Documents" / "net-distance-ph"
    workspace = _guard_path(args.workspace or default_workspace, repo, kind="Workspace")
    if args.run and not (args.all and args.execute):
        raise ValueError("--run requires --all --execute")
    if args.purge_patch_artifacts and not args.all:
        raise ValueError("--purge-patch-artifacts requires --all")
    if args.workers < 1 or not math.isfinite(args.heartbeat_seconds) or args.heartbeat_seconds <= 0:
        raise ValueError("Workers must be >=1 and heartbeat seconds must be finite and >0")
    branch = None
    commit = None
    if args.all:
        branch, commit = _assert_pushed_clean(repo, fetch=False)
        # Never delete the active interpreter/virtual environment with git clean.
        interpreter = Path(sys.executable).resolve()
        if interpreter == repo or repo in interpreter.parents:
            raise RuntimeError("Python interpreter is inside repository; move the environment before --all")
    patches = []
    if args.purge_patch_artifacts:
        patches = [_safe_patch_path(p, repo, home) for p in _patch_paths(repo, home) if p.exists()]
    _say(f"Repository: {repo}")
    if args.all:
        _say(f"Clean pushed branch: {branch} @ {commit[:12]}")
        preview = _git(repo, "clean", "-ndffx")
        if preview:
            lines = preview.splitlines()
            _say(f"Generated/ignored repository items to remove ({len(lines)} top-level entries):")
            for item in lines[:50]:
                print("  " + item, flush=True)
            if len(lines) > 50:
                _say(f"  ... and {len(lines) - 50} more entries")
        else:
            _say("No ignored/untracked repository files to remove")
    _say(f"Workspace to delete: {workspace}")
    if workspace.exists():
        names = sorted(x.name for x in workspace.iterdir())
        _say(f"Workspace contains {len(names)} top-level entries: {', '.join(names[:30])}")
    else:
        _say("Workspace does not exist; there is nothing there to delete")
    if args.purge_patch_artifacts:
        _say(f"Historical patch artifacts to delete ({len(patches)}):")
        for p in patches:
            print("  " + str(p), flush=True)
    if args.run:
        _say("Rebuild afterward: prepare -> graph -> population-weighted (balanced, faeldon)")
    if not args.execute:
        _say("PREVIEW ONLY. Nothing deleted. Add --execute after checking the inventory.")
        return 0
    # Refresh AFTER preview and BEFORE the irreversible action. A change on
    # the remote must not silently change which commit is used.
    if args.all:
        checked_branch, checked_commit = _assert_pushed_clean(repo, fetch=True)
        if (branch, commit) != (checked_branch, checked_commit):
            raise RuntimeError("Upstream changed during preflight; rerun preview")
    print("\nPERMANENT DELETE: the workspace" +
          (", ignored/generated checkout files" if args.all else "") +
          (", and listed historical patch artifacts" if patches else ""))
    print(f"Type exactly: {CONFIRM}")
    if input("Confirmation: ").strip() != CONFIRM:
        _say("Cancelled. Nothing deleted.")
        return 1
    if args.all:
        _git(repo, "reset", "--hard", f"refs/remotes/origin/{branch}")
        _git(repo, "clean", "-ffdx")
        if _git(repo, "status", "--porcelain", "--untracked-files=all"):
            raise RuntimeError("Repository did not become clean; stopping before workspace deletion")
        _say("Git checkout matches pushed branch; generated checkout files removed")
    if workspace.exists():
        shutil.rmtree(workspace)
    _say("Computational workspace removed")
    for path in patches:
        _delete_path(path)
    if patches:
        _say("Listed historical patch artifacts removed")
    if args.run:
        return _run_from_zero(repo, workspace, branch=branch, commit=commit,
                              workers=args.workers, heartbeat=args.heartbeat_seconds)
    return 0
