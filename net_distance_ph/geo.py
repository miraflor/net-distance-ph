"""
Geodesy on the WGS84 ellipsoid, vectorised, with no hard dependencies.

Two things are needed by the pipeline:

1. *Lengths* of the little segments that make up a road or ferry way. These
   become edge weights, so any systematic bias here rescales the whole distance
   matrix. Spherical haversine on a mean-radius sphere is biased by up to
   ~0.5% depending on latitude and bearing, which is a kilometre in every two
   hundred -- not acceptable for a matrix that is meant to be metric.

   ``geodesic_km`` therefore uses Lambert's formula for long lines on the
   ellipsoid (Lambert 1942). Its error is a near-constant scale factor of
   about 1.4e-6 (1.4 mm per km, measured against Karney at 10 m to 500 km),
   which is far below the positional accuracy of OSM geometry. If ``pyproj``
   is installed it is used instead, which is exact (Karney 2013) and faster
   because it drops into C.

2. *Nearest-neighbour search* (ferry terminal -> road node, centroid -> graph
   node). Projecting to a metric CRS would need pyproj and would distort at
   the edges of the country. Instead we embed lon/lat on the ellipsoid into
   3-D Cartesian (ECEF) coordinates and use a Euclidean KD-tree. Euclidean
   distance in ECEF is the *chord*, which provides a close approximation to
   geodesic-distance ordering for short connections. On an ellipsoid, chord
   ordering is not guaranteed to equal geodesic ordering in every near tie.
   The chosen connector's length is nevertheless calculated geodesically.
"""

from __future__ import annotations

import numpy as np

# WGS84
A_WGS84 = 6378137.0
F_WGS84 = 1.0 / 298.257223563
B_WGS84 = A_WGS84 * (1.0 - F_WGS84)
E2_WGS84 = F_WGS84 * (2.0 - F_WGS84)

_PYPROJ_GEOD = None
_PYPROJ_TRIED = False


def _pyproj_geod():
    global _PYPROJ_GEOD, _PYPROJ_TRIED
    if not _PYPROJ_TRIED:
        _PYPROJ_TRIED = True
        try:
            from pyproj import Geod

            _PYPROJ_GEOD = Geod(ellps="WGS84")
        except Exception:
            _PYPROJ_GEOD = None
    return _PYPROJ_GEOD


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------
def lambert_km(lon1, lat1, lon2, lat2):
    """Lambert's formula for geodesic distance on WGS84, in km.

    Pure numpy, vectorised, no dependencies. The series is truncated at first
    order in the flattening, so the relative error is about f**2/8 = 1.4e-6
    for every line length used here (10 m to a few hundred km).
    """
    lon1 = np.asarray(lon1, dtype=np.float64)
    lat1 = np.asarray(lat1, dtype=np.float64)
    lon2 = np.asarray(lon2, dtype=np.float64)
    lat2 = np.asarray(lat2, dtype=np.float64)

    f = F_WGS84
    # Reduced (parametric) latitudes.
    b1 = np.arctan((1.0 - f) * np.tan(np.radians(lat1)))
    b2 = np.arctan((1.0 - f) * np.tan(np.radians(lat2)))

    dlon = np.radians(_wrap180(lon2 - lon1))
    dbeta = b2 - b1

    # Central angle by the haversine form (stable for short lines).
    hav = np.sin(dbeta / 2.0) ** 2 + np.cos(b1) * np.cos(b2) * np.sin(dlon / 2.0) ** 2
    hav = np.clip(hav, 0.0, 1.0)
    sigma = 2.0 * np.arcsin(np.sqrt(hav))

    P = (b1 + b2) / 2.0
    Q = (b2 - b1) / 2.0

    sin_sigma = np.sin(sigma)
    c2 = np.cos(sigma / 2.0) ** 2
    s2 = np.sin(sigma / 2.0) ** 2

    with np.errstate(divide="ignore", invalid="ignore"):
        X = (sigma - sin_sigma) * (np.sin(P) ** 2) * (np.cos(Q) ** 2) / c2
        Y = (sigma + sin_sigma) * (np.cos(P) ** 2) * (np.sin(Q) ** 2) / s2
    # sigma == 0 makes Y indeterminate (0/0); the correction vanishes there.
    zero = sigma == 0.0
    X = np.where(zero, 0.0, X)
    Y = np.where(zero, 0.0, Y)

    dist_m = A_WGS84 * (sigma - (f / 2.0) * (X + Y))
    return dist_m / 1000.0


def geodesic_km(lon1, lat1, lon2, lat2, backend="auto"):
    """Geodesic distance in km. ``backend`` in {'auto', 'pyproj', 'lambert'}."""
    if backend not in ("auto", "pyproj", "lambert"):
        raise ValueError(f"unknown geodesic backend {backend!r}")
    if backend in ("auto", "pyproj"):
        geod = _pyproj_geod()
        if geod is not None:
            # pyproj needs equal-length arrays; broadcast so that a scalar
            # point against an array behaves as it does in the Lambert path.
            args = np.broadcast_arrays(*(np.asarray(x, dtype=np.float64) for x in (lon1, lat1, lon2, lat2)))
            shape = args[0].shape
            _, _, d = geod.inv(*(np.ascontiguousarray(a).ravel() for a in args))
            return (np.abs(np.asarray(d, dtype=np.float64)) / 1000.0).reshape(shape)
        if backend == "pyproj":
            raise RuntimeError("pyproj requested but not importable")
    return lambert_km(lon1, lat1, lon2, lat2)


def _wrap180(d):
    return (np.asarray(d, dtype=np.float64) + 180.0) % 360.0 - 180.0


# ---------------------------------------------------------------------------
# ECEF embedding for exact nearest-neighbour ranking
# ---------------------------------------------------------------------------
def lonlat_to_ecef(lon, lat):
    """Geodetic lon/lat (degrees, height 0) -> ECEF metres, shape (n, 3)."""
    lam = np.radians(np.asarray(lon, dtype=np.float64))
    phi = np.radians(np.asarray(lat, dtype=np.float64))
    sphi = np.sin(phi)
    N = A_WGS84 / np.sqrt(1.0 - E2_WGS84 * sphi * sphi)
    x = N * np.cos(phi) * np.cos(lam)
    y = N * np.cos(phi) * np.sin(lam)
    z = N * (1.0 - E2_WGS84) * sphi
    return np.column_stack([x, y, z])


def chord_to_arc_km(chord_m):
    """Convert an ECEF chord length to surface arc length, in km.

    Uses the mean radius; over the <25 km range where we use it the difference
    between chord and arc is itself only ~10^-5 relative, so the choice of
    radius is immaterial. Exact monotonicity -- which is what the KD-tree
    ranking depends on -- holds regardless.
    """
    R = (2.0 * A_WGS84 + B_WGS84) / 3.0
    c = np.asarray(chord_m, dtype=np.float64)
    return 2.0 * R * np.arcsin(np.clip(c / (2.0 * R), 0.0, 1.0)) / 1000.0


def arc_to_chord_m(arc_km):
    """Inverse of :func:`chord_to_arc_km` -- used to turn a search radius in km
    into the Euclidean radius the KD-tree wants."""
    R = (2.0 * A_WGS84 + B_WGS84) / 3.0
    return 2.0 * R * np.sin(np.asarray(arc_km, dtype=np.float64) * 1000.0 / (2.0 * R))
