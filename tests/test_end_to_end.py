"""
End-to-end test on a miniature archipelago.

Two islands, each with a road grid; a ferry route between them whose terminals
are digitised ~1 km offshore of the road ends (the realistic case); a third
island with a road grid and no ferry at all; and one town parked in the middle
of nowhere. Roads are densified with shape points so the contraction has
something to chew on.

The test asserts the property that actually matters: the matrix produced by the
whole fast pipeline equals the matrix from a plain Dijkstra on the unreduced
graph, and is exactly symmetric.
"""

import csv
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.sparse.csgraph import dijkstra

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mkshp import write_dbf, write_point_shp, write_polyline_shp

from ph_net_distance.matrix import build_csr
from ph_net_distance.reduce import dedupe_edges

# A label with a comma and a double quote, to check that towns.csv keeps it exactly.
AWKWARD_NAME = 'Town, "Three"'


def _densify(p, q, k):
    """Split a segment into k pieces, giving a chain of degree-2 shape points."""
    t = np.linspace(0, 1, k + 1)
    return [(p[0] + (q[0] - p[0]) * s, p[1] + (q[1] - p[1]) * s) for s in t]


def _island(lon0, lat0, nx, ny, step, dens=6):
    """Grid of roads, each edge densified. Returns list of polyline records."""
    recs = []
    for i in range(nx):
        for j in range(ny):
            p = (lon0 + i * step, lat0 + j * step)
            if i + 1 < nx:
                recs.append([_densify(p, (lon0 + (i + 1) * step, lat0 + j * step), dens)])
            if j + 1 < ny:
                recs.append([_densify(p, (lon0 + i * step, lat0 + (j + 1) * step), dens)])
    return recs


def write_ferry(data_dir, detour=0.02):
    """Ferry: from ~1 km east of A's east edge to ~1 km west of B's west edge,
    so gangway edges are required at both ends. Multipart, to be nasty.
    ``detour`` moves the shared mid point north, lengthening the crossing."""
    step = 0.05
    a_end = (120.00 + 5 * step + 0.010, 14.00 + 2 * step)
    b_end = (121.20 - 0.010, 14.10 + 2 * step)
    mid = ((a_end[0] + b_end[0]) / 2, (a_end[1] + b_end[1]) / 2 + detour)
    write_polyline_shp(
        Path(data_dir) / "ferry_route_shape_shp" / "ferry_route_shape.shp",
        [[_densify(a_end, mid, 4)], [_densify(mid, b_end, 4)]],
        shape_type=13,
    )


def build_fixture(data_dir):
    data_dir = Path(data_dir)
    (data_dir / "primsectertrunk_road_shp").mkdir(parents=True)
    (data_dir / "ferry_route_shape_shp").mkdir(parents=True)
    (data_dir / "muncity_centroids_shp").mkdir(parents=True)

    step = 0.05
    A = _island(120.00, 14.00, 6, 5, step)   # island A
    B = _island(121.20, 14.10, 5, 5, step)   # island B
    Cc = _island(122.60, 13.50, 3, 3, step)  # island C: unreachable, no ferry
    roads = A + B + Cc
    write_polyline_shp(data_dir / "primsectertrunk_road_shp" / "primsectertrunk_road.shp",
                       roads, shape_type=13)
    write_ferry(data_dir)

    towns = []
    for i in (0, 2, 5):
        for j in (0, 4):
            towns.append((120.00 + i * step + 0.004, 14.00 + j * step + 0.004))
    for i in (0, 4):
        for j in (0, 2, 4):
            towns.append((121.20 + i * step + 0.004, 14.10 + j * step + 0.004))
    towns.append((122.60 + 0.004, 13.50 + 0.004))          # island C
    towns.append((122.62 + 0.004, 13.55 + 0.004))          # island C
    towns.append((126.00, 8.00))                            # nowhere: unsnappable
    cent = data_dir / "muncity_centroids_shp" / "muncity_centroids.shp"
    write_point_shp(cent, towns, shape_type=11)
    write_dbf(
        cent.with_suffix(".dbf"),
        [("PSGC10", "C", 10, 0), ("ADM3_EN", "C", 24, 0)],
        [[f"{i:010d}", AWKWARD_NAME if i == 3 else f"Town {i}"] for i in range(len(towns))],
    )
    return len(towns)


def _env(tmp_path):
    env = os.environ.copy()
    env.update({
        "PH_NET_DISTANCE_DATA": str(tmp_path / "data"),
        "PH_NET_DISTANCE_WORK": str(tmp_path / "work"),
        "PH_NET_DISTANCE_OUT": str(tmp_path / "out"),
    })
    return env


def _run(tmp_path, *args):
    project = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, str(project / "run.py"), *args, "--workers", "1"],
        cwd=project, env=_env(tmp_path), capture_output=True, text=True, timeout=180,
    )


def reference_matrix(work_dir, n_towns):
    """Plain Dijkstra on the unreduced graph, for comparison."""
    g = np.load(Path(work_dir) / "graph_full.npz")
    s = np.load(Path(work_dir) / "sources.npz")
    n = g["node_lonlat"].shape[0]
    u, v, w = dedupe_edges(g["u"], g["v"], g["w"], n)
    G = build_csr(u, v, w, n)
    node_idx, offset, tier = s["node_idx"], s["offset"], s["tier"]
    snapped = tier >= 0
    D = np.full((n_towns, n_towns), np.inf)
    ok = np.flatnonzero(snapped)
    sub = dijkstra(G, directed=False, indices=node_idx[ok])[:, node_idx[ok]]
    D[np.ix_(ok, ok)] = sub
    add = offset[:, None] + offset[None, :]
    fin = np.isfinite(D)
    D[fin] += add[fin]
    np.fill_diagonal(D, 0.0)
    return D


def test_end_to_end_pipeline(tmp_path):
    """Execute the actual CLI on synthetic data, with no private paths."""
    n_towns = build_fixture(tmp_path / "data")
    result = _run(tmp_path, "all")
    assert result.returncode == 0, result.stdout + "\n" + result.stderr

    output = tmp_path / "out"
    distances = np.load(output / "distance_matrix_km.npy")
    expected = reference_matrix(tmp_path / "work", n_towns)
    assert distances.shape == (n_towns, n_towns)
    assert np.array_equal(np.isfinite(distances), np.isfinite(expected))
    finite = np.isfinite(distances)
    assert np.max(np.abs(distances[finite] - expected[finite])) < 1e-9
    assert np.array_equal(distances, distances.T)
    assert (np.diag(distances) == 0).all()
    assert np.isinf(distances[0, -2])
    assert np.isinf(distances[-1, 0])
    assert np.isfinite(distances[0, 6]) and distances[0, 6] > 50
    assert np.isfinite(distances[-2, -3])
    assert "RESULT: all checks passed" in (output / "verification.txt").read_text()

    # With the connector stubs included, the matrix is still a metric:
    # check the triangle inequality on every triple, not a sample.
    D = np.where(finite, distances, np.inf)
    via = D[:, :, None] + D[None, :, :]          # via[a, b, c] = d(a,b) + d(b,c)
    assert np.all(D[:, None, :] <= via + 1e-9)    # d(a,c) <= d(a,b) + d(b,c)

    # towns.csv is real CSV: an awkward label survives a round trip exactly.
    with (output / "towns.csv").open(encoding="utf-8", newline="") as fh:
        towns = list(csv.DictReader(fh))
    assert towns[3]["name"] == AWKWARD_NAME
    assert [t["id"] for t in towns] == [f"{i:010d}" for i in range(n_towns)]


def test_rerun_after_input_change_is_not_stale(tmp_path):
    """Re-running in the same work/ after new inputs must give the new distances.

    Earlier versions resumed the old matrix checkpoint because only its shape
    was checked, and wrote the old distances to out/.
    """
    n_towns = build_fixture(tmp_path / "data")
    assert _run(tmp_path, "all").returncode == 0
    before = np.load(tmp_path / "out" / "distance_matrix_km.npy")

    write_ferry(tmp_path / "data", detour=0.30)   # a much longer crossing
    result = _run(tmp_path, "all")
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    after = np.load(tmp_path / "out" / "distance_matrix_km.npy")
    expected = reference_matrix(tmp_path / "work", n_towns)
    finite = np.isfinite(expected)
    assert np.max(np.abs(after[finite] - expected[finite])) < 1e-9
    assert after[0, 6] > before[0, 6] + 5      # the cross-island trip got longer


def test_stages_from_different_runs_are_not_mixed(tmp_path):
    """`graph` with new points followed by `matrix` (no `reduce`) must stop.

    Earlier versions combined the new IDs with the old core and exited 0,
    attaching distances to the wrong localities.
    """
    build_fixture(tmp_path / "data")
    assert _run(tmp_path, "all").returncode == 0
    write_ferry(tmp_path / "data", detour=0.30)
    assert _run(tmp_path, "graph").returncode == 0
    result = _run(tmp_path, "matrix")
    assert result.returncode != 0
    assert "python run.py reduce" in result.stdout + result.stderr
    # verify also refuses, rather than checking unrelated files against each other
    result = _run(tmp_path, "verify")
    assert result.returncode != 0


if __name__ == "__main__":
    raise SystemExit("Use python -m pytest tests/test_end_to_end.py -q")
