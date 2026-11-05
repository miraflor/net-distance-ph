"""Tiny shapefile writer -- test fixtures only, not part of the pipeline."""

import struct
from pathlib import Path


def _bbox(all_pts):
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    return min(xs), min(ys), max(xs), max(ys)


def write_polyline_shp(path, records, shape_type=3):
    """records: list of list-of-parts, each part a list of (x, y)."""
    path = Path(path)
    contents = []
    for parts in records:
        pts = [p for part in parts for p in part]
        n_parts, n_pts = len(parts), len(pts)
        idx, acc = [], 0
        for part in parts:
            idx.append(acc)
            acc += len(part)
        x0, y0, x1, y1 = _bbox(pts)
        body = struct.pack("<i", shape_type)
        body += struct.pack("<4d", x0, y0, x1, y1)
        body += struct.pack("<2i", n_parts, n_pts)
        body += struct.pack(f"<{n_parts}i", *idx)
        for x, y in pts:
            body += struct.pack("<2d", x, y)
        if shape_type == 13:  # PolyLineZ: Z range + Z array, M range + M array
            body += struct.pack("<2d", 0.0, 0.0) + struct.pack(f"<{n_pts}d", *([0.0] * n_pts))
            body += struct.pack("<2d", 0.0, 0.0) + struct.pack(f"<{n_pts}d", *([0.0] * n_pts))
        contents.append(body)
    _emit(path, contents, shape_type, [p for r in records for pt in r for p in pt])


def write_point_shp(path, pts, shape_type=1):
    contents = []
    for x, y in pts:
        body = struct.pack("<i", shape_type) + struct.pack("<2d", x, y)
        if shape_type == 11:
            body += struct.pack("<2d", 0.0, 0.0)
        contents.append(body)
    _emit(path, contents, shape_type, pts)


def _emit(path, contents, shape_type, all_pts):
    shp, shx = bytearray(), bytearray()
    header = bytearray(100)
    struct.pack_into(">i", header, 0, 9994)
    struct.pack_into("<i", header, 28, 1000)
    struct.pack_into("<i", header, 32, shape_type)
    x0, y0, x1, y1 = _bbox(all_pts)
    struct.pack_into("<4d", header, 36, x0, y0, x1, y1)
    shp += header
    shx += header

    offset_words = 50
    for i, body in enumerate(contents):
        clen = len(body) // 2
        shp += struct.pack(">2i", i + 1, clen) + body
        shx += struct.pack(">2i", offset_words, clen)
        offset_words += 4 + clen

    struct.pack_into(">i", shp, 24, len(shp) // 2)
    struct.pack_into(">i", shx, 24, len(shx) // 2)
    Path(path).write_bytes(bytes(shp))
    Path(path).with_suffix(".shx").write_bytes(bytes(shx))


def write_dbf(path, columns, rows):
    """columns: list of (name, type_char, length, decimals); rows: list of lists."""
    n_rec = len(rows)
    rec_len = 1 + sum(c[2] for c in columns)
    hdr_len = 32 + 32 * len(columns) + 1
    out = bytearray()
    out += struct.pack("<B3B", 0x03, 26, 1, 1)
    out += struct.pack("<I", n_rec)
    out += struct.pack("<HH", hdr_len, rec_len)
    out += bytes(20)
    for name, typ, length, dec in columns:
        out += name.encode("ascii")[:11].ljust(11, b"\x00")
        out += typ.encode("ascii")
        out += bytes(4)
        out += bytes([length, dec])
        out += bytes(14)
    out += b"\x0d"
    for row in rows:
        out += b" "
        for (name, typ, length, dec), val in zip(columns, row):
            s = str(val).encode("utf-8")[:length]
            out += s.rjust(length) if typ in "NF" else s.ljust(length)
    out += b"\x1a"
    Path(path).write_bytes(bytes(out))
