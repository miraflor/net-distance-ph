"""Invalid runtime parameters must fail at the command boundary."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from phnetdist.matrix import compute

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("args,expected", [
    (["--batch", "0"], "--batch must be"),
    (["--workers", "-1"], "--workers must be"),
    (["--wave-scale", "nan"], "--wave-scale must be"),
    (["--wave-scale", "0"], "--wave-scale must be"),
])
def test_cli_rejects_bad_numeric_options(args, expected):
    result = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "graph", *args],
        capture_output=True, text=True, cwd=ROOT, timeout=20,
    )
    assert result.returncode != 0
    assert expected in result.stderr


@pytest.mark.parametrize("kwargs", [
    {"batch_size": 0},
    {"n_workers": -1},
    {"checkpoint_every": 0},
])
def test_compute_rejects_invalid_parameters_before_touching_files(tmp_path, kwargs):
    with pytest.raises(ValueError):
        compute(tmp_path, [], verbose=False, **kwargs)
    assert not list(tmp_path.iterdir())
