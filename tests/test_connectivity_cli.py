"""Integration smoke checks for the opt-in first-class CLI stage."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_run_cli_registers_connectivity():
    import run
    assert 'connectivity' in run.STAGES
    assert 'graph' in run.STAGES
    assert 'matrix' in run.STAGES
    assert 'reduce' in run.STAGES
    assert 'verify' in run.STAGES


def test_help_advertises_connectivity():
    repo = Path(__file__).resolve().parents[1]
    process = subprocess.run([sys.executable, str(repo / 'run.py'), '--help'],
                             cwd=repo, text=True, capture_output=True, timeout=30)
    assert process.returncode == 0
    assert 'connectivity' in process.stdout
