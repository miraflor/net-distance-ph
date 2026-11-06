import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mkshp import write_dbf, write_point_shp, write_polyline_shp

from net_distance_ph.shpio import read_dbf, read_shapefile_parts, read_shapefile_points


def test_polyline_roundtrip_singlepart():
    recs = [
        [[(120.0, 14.0), (120.1, 14.1), (120.2, 14.05)]],
        [[(121.0, 15.0), (121.5, 15.5)]],
    ]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "l.shp"
        write_polyline_shp(p, recs)
        parts = read_shapefile_parts(p)
    assert parts.n_records == 2
    assert parts.xy.shape == (5, 2)
    assert list(parts.part_offset) == [0, 3, 5]
    assert list(parts.part_record) == [0, 1]
    i, j, _ = parts.segment_endpoints()
    assert list(i) == [0, 1, 3] and list(j) == [1, 2, 4]
    print("  single-part ok")


def test_polyline_multipart_no_phantom_edge():
    """A two-part record must not produce an edge across the part boundary."""
    recs = [[[(120.0, 14.0), (120.1, 14.0)], [(125.0, 6.0), (125.1, 6.0)]]]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "l.shp"
        write_polyline_shp(p, recs)
        parts = read_shapefile_parts(p)
    assert list(parts.part_offset) == [0, 2, 4]
    i, j, pr = parts.segment_endpoints()
    assert list(i) == [0, 2] and list(j) == [1, 3]
    assert list(parts.part_record) == [0, 0]
    print("  multipart ok, no 500 km phantom edge")


def test_polylinez_and_missing_shx():
    recs = [[[(120.0, 14.0), (120.1, 14.1)]], [[(120.2, 14.2), (120.3, 14.3), (120.4, 14.1)]]]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "z.shp"
        write_polyline_shp(p, recs, shape_type=13)
        a = read_shapefile_parts(p)
        p.with_suffix(".shx").unlink()
        b = read_shapefile_parts(p)
    assert np.array_equal(a.xy, b.xy)
    assert np.allclose(a.xy[-1], [120.4, 14.1])
    print("  PolyLineZ ok; .shx-less fallback agrees")


def test_points_and_dbf():
    pts = [(120.98, 14.6), (123.89, 10.31), (125.6, 7.07)]
    cols = [("PSGC", "C", 10, 0), ("NAME", "C", 20, 0), ("POP", "N", 12, 0)]
    rows = [
        ["1339000000", "Quezon City", 2960048],
        ["0722200000", "Cebu City", 964169],
        ["1102400000", "Davao City", 1776949],
    ]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.shp"
        write_point_shp(p, pts, shape_type=11)
        write_dbf(p.with_suffix(".dbf"), cols, rows)
        xy = read_shapefile_points(p)
        tbl, n = read_dbf(p.with_suffix(".dbf"), ["PSGC", "NAME", "POP"])
    assert np.allclose(xy, np.array(pts))
    assert n == 3
    assert list(tbl["PSGC"]) == [r[0] for r in rows]
    assert tbl["PSGC"][0] == "1339000000", "leading zeros must survive"
    assert list(tbl["NAME"]) == [r[1] for r in rows]
    print("  points + dbf ok; PSGC leading zeros preserved")


def test_unaligned_offsets():
    """Record contents rarely land on 8-byte boundaries; frombuffer must cope."""
    recs = [[[(120.0 + k * 0.01, 14.0)] * 1 + [(120.5, 14.5)]] for k in range(37)]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "u.shp"
        write_polyline_shp(p, recs)
        parts = read_shapefile_parts(p)
    assert parts.xy.shape == (74, 2)
    assert np.isclose(parts.xy[0, 0], 120.0)
    print("  unaligned record offsets ok")


def _append_null_record(shp_path):
    """Append a Null-shape record (type 0, 4 bytes of content) to .shp and .shx."""
    shp = bytearray(shp_path.read_bytes())
    shx = bytearray(shp_path.with_suffix(".shx").read_bytes())
    body = struct.pack("<i", 0)
    offset_words = len(shp) // 2
    shp += struct.pack(">2i", 99, len(body) // 2) + body
    shx += struct.pack(">2i", offset_words, len(body) // 2)
    struct.pack_into(">i", shp, 24, len(shp) // 2)
    struct.pack_into(">i", shx, 24, len(shx) // 2)
    shp_path.write_bytes(bytes(shp))
    shp_path.with_suffix(".shx").write_bytes(bytes(shx))


def test_null_shape_as_last_record():
    """A Null record holds no header fields; reading them used to run past EOF."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "n.shp"
        write_polyline_shp(p, [[[(120.0, 14.0), (120.1, 14.1)]]])
        _append_null_record(p)
        with_shx = read_shapefile_parts(p)
        p.with_suffix(".shx").unlink()
        without_shx = read_shapefile_parts(p)
    for parts in (with_shx, without_shx):
        assert parts.n_records == 2
        assert parts.xy.shape == (2, 2)
        assert list(parts.part_record) == [0]


def test_numeric_code_field_with_blank_reads_as_float():
    """The reader reports this case as floats; run.py then rejects the column."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "num.dbf"
        write_dbf(f, [("PSGC", "N", 10, 0)], [["1380600000"], [""]])
        tbl, _ = read_dbf(f, ["PSGC"])
    assert tbl["PSGC"].dtype == np.float64


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("OK")
