#!/usr/bin/env python3
"""Show the latest stage-2 heartbeat and checkpoint state."""

from __future__ import annotations

import argparse
from pathlib import Path
import time

import numpy as np

import config as C


def age_text(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.1f} minutes"
    return f"{seconds / 3600:.1f} hours"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=None,
                        help="intermediate directory used by run.py (or set NET_DISTANCE_PH_WORK)")
    args = parser.parse_args()
    work = (args.work.expanduser().resolve() if args.work is not None else Path(C.WORK))
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
        print("STATUS: An unfinished but resumable stage-2 checkpoint exists.")
        print(f"Checkpoint last written {age_text(age)} ago.")
        try:
            with np.load(checkpoint, allow_pickle=False) as cp:
                alive = cp["alive"]
                n_nodes = int(cp["n_nodes"])
                live = int(alive.sum()) if alive.size else n_nodes
                print(f"Live nodes at checkpoint: {live:,}")
                print(f"Edges at checkpoint: {cp['u'].size:,}")
                print(f"Completed contraction passes: {int(cp['completed_passes']):,}")
                print(f"Next phase index: {int(cp['next_phase'])}")
                print(f"Completed passes in current phase: {int(cp['next_pass'])}")
        except Exception as exc:
            print(f"Could not read checkpoint details: {exc}")
    else:
        print("STATUS: No stage-2 checkpoint or completed core was found.")

    if log.exists():
        age = time.time() - log.stat().st_mtime
        print()
        print(f"Progress log last changed {age_text(age)} ago.")
        likely_window = max(float(C.REDUCE_HEARTBEAT_SECONDS) * 2.5, 75.0)
        if age <= likely_window:
            print("A recent heartbeat exists, so Step 2 is likely running or just checkpointing.")
        else:
            print(
                "No recent heartbeat. The process may be sleeping, stopped, or between "
                "heartbeat-covered operations. The checkpoint remains resumable."
            )
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
