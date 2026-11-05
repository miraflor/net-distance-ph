import math

import numpy as np

from ph_net_distance.geo import (
    A_WGS84,
    B_WGS84,
    F_WGS84,
    arc_to_chord_m,
    chord_to_arc_km,
    lambert_km,
    lonlat_to_ecef,
)


def vincenty_inverse_m(lon1, lat1, lon2, lat2):
    """Reference implementation (Vincenty 1975), scalar, high accuracy."""
    a, b, f = A_WGS84, B_WGS84, F_WGS84
    L = math.radians(lon2 - lon1)
    U1 = math.atan((1 - f) * math.tan(math.radians(lat1)))
    U2 = math.atan((1 - f) * math.tan(math.radians(lat2)))
    sU1, cU1 = math.sin(U1), math.cos(U1)
    sU2, cU2 = math.sin(U2), math.cos(U2)
    lam = L
    for _ in range(200):
        sl, cl = math.sin(lam), math.cos(lam)
        ss = math.sqrt((cU2 * sl) ** 2 + (cU1 * sU2 - sU1 * cU2 * cl) ** 2)
        if ss == 0:
            return 0.0
        cs = sU1 * sU2 + cU1 * cU2 * cl
        sigma = math.atan2(ss, cs)
        sa = cU1 * cU2 * sl / ss
        c2a = 1 - sa * sa
        c2sm = cs - 2 * sU1 * sU2 / c2a if c2a != 0 else 0.0
        C = f / 16 * c2a * (4 + f * (4 - 3 * c2a))
        lam_prev = lam
        lam = L + (1 - C) * f * sa * (
            sigma + C * ss * (c2sm + C * cs * (-1 + 2 * c2sm**2))
        )
        if abs(lam - lam_prev) < 1e-14:
            break
    u2 = c2a * (a * a - b * b) / (b * b)
    A = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))
    B = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)))
    dsig = (
        B
        * ss
        * (
            c2sm
            + B
            / 4
            * (
                cs * (-1 + 2 * c2sm**2)
                - B / 6 * c2sm * (-3 + 4 * ss**2) * (-3 + 4 * c2sm**2)
            )
        )
    )
    return b * A * (sigma - dsig)


def _offset(lon, lat, dist_km, bearing):
    """Move a point along a bearing using local radii of curvature.

    Only used to *generate* test pairs; accuracy of the generator is
    irrelevant since both formulas are evaluated on the same endpoints.
    """
    a, e2 = A_WGS84, F_WGS84 * (2 - F_WGS84)
    phi = np.radians(lat)
    W = np.sqrt(1 - e2 * np.sin(phi) ** 2)
    M = a * (1 - e2) / W**3
    N = a / W
    dn = dist_km * 1000 * np.cos(bearing)
    de = dist_km * 1000 * np.sin(bearing)
    return lon + np.degrees(de / (N * np.cos(phi))), lat + np.degrees(dn / M)


def test_lambert_accuracy():
    """Lambert vs Vincenty over the segment lengths that occur in the data.

    Lambert's series truncates at O(f), so the residual is bounded by about
    f**2/8 = 1.4e-6 relative -- 1.4 mm per km. Two orders of magnitude below
    the positional accuracy of OSM geometry, and a pure scale effect, so it
    cannot distort the *shape* of the matrix.
    """
    rng = np.random.default_rng(0)
    for scale_km in (0.01, 0.1, 1.0, 10.0, 100.0, 200.0):
        lat1 = rng.uniform(4.5, 21.0, 400)
        lon1 = rng.uniform(116.0, 127.0, 400)
        bearing = rng.uniform(0, 2 * np.pi, 400)
        lon2, lat2 = _offset(lon1, lat1, scale_km, bearing)

        got = lambert_km(lon1, lat1, lon2, lat2)
        ref = np.array(
            [
                vincenty_inverse_m(a, b, c, d) / 1000.0
                for a, b, c, d in zip(lon1, lat1, lon2, lat2)
            ]
        )
        rel = np.max(np.abs(got - ref) / np.maximum(ref, 1e-12))
        absm = np.max(np.abs(got - ref)) * 1000.0
        print(f"  {scale_km:>7.2f} km segments: max rel {rel:.2e}  max abs {absm:.4f} m")
        assert rel < 2e-6, f"Lambert too inaccurate at {scale_km} km: {rel}"


def test_lambert_zero_and_symmetry():
    assert lambert_km(121.0, 14.6, 121.0, 14.6) == 0.0
    a = lambert_km(121.0, 14.6, 123.9, 10.3)
    b = lambert_km(123.9, 10.3, 121.0, 14.6)
    assert abs(a - b) < 1e-12
    print(f"  Manila-Cebu (straight geodesic): {a:.3f} km, symmetric to {abs(a-b):.1e}")


def test_ecef_chord_ranking_is_exact():
    """The point of ECEF: chord ordering == surface-distance ordering."""
    rng = np.random.default_rng(1)
    lon0, lat0 = 121.0, 14.6
    lon = lon0 + rng.uniform(-1.0, 1.0, 2000)
    lat = lat0 + rng.uniform(-1.0, 1.0, 2000)
    P = lonlat_to_ecef(lon, lat)
    p0 = lonlat_to_ecef([lon0], [lat0])[0]
    chord = np.linalg.norm(P - p0, axis=1)
    surf = lambert_km(lon0, lat0, lon, lat)
    assert np.array_equal(np.argsort(chord), np.argsort(surf))
    # And the chord->arc conversion recovers the surface distance closely.
    arc = chord_to_arc_km(chord)
    rel = np.max(np.abs(arc - surf) / np.maximum(surf, 1e-9))
    print(f"  chord->arc vs geodesic: max rel err {rel:.3e}")
    assert rel < 3e-3  # ellipsoidal flattening, not a ranking error
    # Radius round-trip.
    assert abs(chord_to_arc_km(arc_to_chord_m(5.0)) - 5.0) < 1e-9


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("OK")
