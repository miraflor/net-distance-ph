"""The documented command sequence and the beginner preflight check."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.test_end_to_end import build_fixture

PROJECT = Path(__file__).resolve().parents[1]


def _clean_env():
    # No PH_NET_DISTANCE_* variables: the data folder is named only on the
    # command line, as in the README and the beginner guide.
    return {k: v for k, v in os.environ.items() if not k.startswith("PH_NET_DISTANCE")}


def _run(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(PROJECT / "run.py"), *args,
         "--work", str(tmp_path / "work"), "--out", str(tmp_path / "out"), "--workers", "1"],
        cwd=PROJECT, env=_clean_env(), capture_output=True, text=True, timeout=180,
    )


def test_later_stages_reuse_the_folder_recorded_by_graph(tmp_path):
    """`graph --data DIR` then `reduce`, `matrix`, `verify` without --data.

    The byte-level input check re-reads the shapefiles in every later stage.
    Before the data folder was recorded, these commands looked in the
    repository's own data/ folder and stopped.
    """
    data = tmp_path / "gis_inputs"
    build_fixture(data)
    result = _run(tmp_path, "graph", "--data", str(data))
    assert result.returncode == 0, result.stdout + result.stderr
    for stage in ("reduce", "matrix", "verify"):
        result = _run(tmp_path, stage)
        assert result.returncode == 0, f"{stage}: " + result.stdout + result.stderr
    assert "RESULT: all checks passed" in (tmp_path / "out" / "verification.txt").read_text()


def test_wrong_explicit_data_folder_gives_a_clear_message(tmp_path):
    data = tmp_path / "gis_inputs"
    build_fixture(data)
    assert _run(tmp_path, "graph", "--data", str(data)).returncode == 0
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    result = _run(tmp_path, "reduce", "--data", str(empty))
    assert result.returncode != 0
    message = result.stdout + result.stderr
    assert "Pass --data with the folder used for `graph`" in message
    assert str(data.resolve()) in message


def test_check_setup_reports_every_file(tmp_path):
    """A missing file is a FAIL line; the other files are still checked."""
    data = tmp_path / "gis_inputs"
    build_fixture(data)
    (data / "ferry_route_shape_shp" / "ferry_route_shape.shp").unlink()
    result = subprocess.run(
        [sys.executable, str(PROJECT / "check_setup.py"), "--data", str(data)],
        cwd=PROJECT, env=_clean_env(), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1
    assert "FOUND: primsectertrunk_road.shp" in result.stdout
    assert "FAIL: ferry_route_shape.shp" in result.stdout
    assert "FOUND: muncity_centroids.shp" in result.stdout
    assert "SETUP CHECK FAILED" in result.stdout
