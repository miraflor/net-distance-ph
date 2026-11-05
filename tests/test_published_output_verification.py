"""The verifier must check the actual published output, not just the core."""
from __future__ import annotations

import numpy as np
from tests.test_end_to_end import _run, build_fixture


def test_symmetric_metric_preserving_corruption_is_rejected(tmp_path):
    build_fixture(tmp_path / "data")
    initial = _run(tmp_path, "all")
    assert initial.returncode == 0, initial.stdout + initial.stderr

    matrix_file = tmp_path / "out" / "distance_matrix_km.npy"
    distances = np.load(matrix_file)
    finite_offdiag = np.isfinite(distances) & ~np.eye(len(distances), dtype=bool)
    distances[finite_offdiag] += 1.0
    np.save(matrix_file, distances)

    result = _run(tmp_path, "verify")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "published matrix matches independently reconstructed distances" in output
    assert "RESULT: FAILED" in output


def test_corrupted_row_validity_radius_is_rejected(tmp_path):
    build_fixture(tmp_path / "data")
    initial = _run(tmp_path, "all")
    assert initial.returncode == 0, initial.stdout + initial.stderr

    radius_file = tmp_path / "out" / "row_validity_radius_km.npy"
    radius = np.load(radius_file)
    radius[0] = 0.0
    np.save(radius_file, radius)

    result = _run(tmp_path, "verify")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "published row-validity radii match" in output
    assert "RESULT: FAILED" in output
