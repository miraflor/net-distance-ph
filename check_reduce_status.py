#!/usr/bin/env python3
"""Show the state of stage 2 (graph reduction) in a work folder.

Stage 2 uses the fast reducer (leaf pruning and degree-2 chain compression).
It writes one checkpoint before the reduction starts: the de-duplicated graph
with components that contain no town removed. It prints progress only between
steps, so a quiet log does not mean that the process stopped.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

import numpy as np

from net_distance_ph.workspace import paths, workspace_path


def age_text(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.1f} minutes"
    return f"{seconds / 3600:.1f} hours"


def default_work() -> Path:
    """The work folder that ``run.py`` and ``net-distance-ph`` use by default."""
    explicit = os.environ.get("NET_DISTANCE_PH_WORK")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return paths(workspace_path())["work"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=None,
                        help="work folder (default: NET_DISTANCE_PH_WORK, else <workspace>/work)")
    args = parser.parse_args()
    work = args.work.expanduser().resolve() if args.work is not None else default_work()
    log = work / "reduce_progress.log"
    checkpoint = work / "reduce_checkpoint.npz"
    core_sources = work / "core_sources.npz"

    print("Stage-2 status")
    print(f"Work folder: {work}")

    if core_sources.exists() and not checkpoint.exists():
        print("STATUS: Stage 2 appears complete.")
        print(f"Completed file: {core_sources}")
    elif checkpoint.exists():
        age = time.time() - checkpoint.stat().st_mtime
        print("STATUS: A stage-2 checkpoint exists (stage 2 is running or was interrupted).")
        print(f"Checkpoint last written {age_text(age)} ago.")
        try:
            with np.load(checkpoint, allow_pickle=False) as cp:
                print(f"Nodes in checkpoint graph: {int(cp['n_nodes']):,}")
                print(f"Edges in checkpoint graph: {cp['u'].size:,}")
        except Exception as exc:
            print(f"Could not read checkpoint details: {exc}")
    else:
        print("STATUS: No stage-2 checkpoint or completed core was found.")

    if log.exists():
        age = time.time() - log.stat().st_mtime
        print()
        print(f"Progress log last changed {age_text(age)} ago.")
        print("The fast reducer logs only between steps; check the process list to see "
              "whether stage 2 is still running.")
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\nLast 12 log lines:")
        for line in lines[-12:]:
            print(line)
    else:
        print("No progress log exists yet.")

    work_flag = f' --work "{work}"' if args.work is not None else ""
    print(f"\nResume command: python run.py reduce{work_flag}")
    print(f"Restart Step 2 from scratch: python run.py reduce --restart-reduce{work_flag}")


if __name__ == "__main__":
    main()
