"""
A minimal ESRI shapefile reader: numpy only, no GDAL, no geopandas, no shapely.

Rationale. The pipeline needs exactly two things out of these files -- the raw
vertex coordinates of each polyline part, and a couple of attribute columns.
Building shapely geometry objects for 126,000 road ways only to throw them away
costs more time and memory than reading the file, and drags in a GDAL stack
that is a common source of install pain. The shapefile format is simple and
fully specified (ESRI 1998), so we read it directly.

What is supported: shape types 1/11/21 (Point) and 3/13/23 (PolyLine), which is
what OSM line and point exports produce. Polygons and multipoints raise.
Z and M values are skipped -- only X/Y are used.

Multipart polylines are split at part boundaries, so no phantom edge is ever
created between the end of one part and the start of the next.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

NULL = 0
POINT_TYPES = {1, 11, 21}
POLYLINE_TYPES = {3, 13, 23}


@dataclass
class Parts:
    """Polyline vertices, concatenated, with part boundaries.

    ``xy[part_offset[k]:part_offset[k + 1]]`` is the vertex list of part k;
    ``part_record[k]`` is the index of the shapefile record it came from.
    """

    xy: np.ndarray  # (M, 2) float64, lon/lat
    part_offset: np.ndarray  # (P + 1,) int64
    part_record: np.ndarray  # (P,) int64
    n_records: int

    def segment_endpoints(self):
        """Index pairs (i, j) into ``xy`` for every consecutive vertex pair."""
        lo = self.part_offset[:-1]
        hi = self.part_offset[1:]
        n_seg = np.maximum(hi - lo - 1, 0)
        total = int(n_seg.sum())
        if total == 0:
            return np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int64)
        # Starting vertex index of each segment, built without a Python loop.
        starts = np.repeat(lo, n_seg)
        within = np.arange(total, dtype=np.int64) - np.repeat(
            np.concatenate([[0], np.cumsum(n_seg)[:-1]]), n_seg
        )
        i = starts + within
        part_of_seg = np.repeat(np.arange(len(n_seg), dtype=np.int64), n_seg)
        return i, i + 1, part_of_seg


def _read_shx_offsets(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    if raw.size < 100:
        raise ValueError(f"{path} is too short to be a .shx")
    body = raw[100:]
    n = body.size // 8
    rec = body[: n * 8].reshape(n, 8)
    off_words = rec[:, :4].copy().view(">u4").ravel().astype(np.int64)
    return off_words * 2  # 16-bit words -> bytes


def read_shapefile_parts(shp_path) -> Parts:
    """Read a PolyLine shapefile into a :class:`Parts`."""
    shp_path = Path(shp_path)
    shx_path = shp_path.with_suffix(".shx")

    raw = np.fromfile(shp_path, dtype=np.uint8)
    buf = raw.data  # memoryview, zero copy
    if raw.size < 100:
        raise ValueError(f"{shp_path} is too short to be a .shp")
    file_type = int(np.frombuffer(raw[32:36].tobytes(), dtype="<i4")[0])

    if file_type in POINT_TYPES:
        raise ValueError(
            f"{shp_path.name} holds points (type {file_type}); "
            "use read_shapefile_points"
        )
    if file_type not in POLYLINE_TYPES and file_type != NULL:
        raise ValueError(
            f"{shp_path.name} has shape type {file_type}; only polylines are "
            "supported by this reader"
        )

    if shx_path.exists():
        offsets = _read_shx_offsets(shx_path)
    else:
        offsets = _scan_offsets(raw)

    n_rec = offsets.size
    # Vectorised read of numParts / numPoints for every record at once.
    # Layout from the record content start: type(4) box(32) numParts(4) numPoints(4)
    stype = raw[(offsets[:, None] + 8 + np.arange(4, dtype=np.int64))]
    stype = stype.copy().view("<i4").ravel()
    # A Null-shape record holds only its 4-byte type, so its header fields must
    # not be read: for the last record in the file they lie beyond its end.
    nonnull = stype != NULL
    hdr = np.zeros((n_rec, 2), dtype="<i4")
    if nonnull.any():
        hdr_idx = offsets[nonnull, None] + 8 + 36 + np.arange(8, dtype=np.int64)
        hdr[nonnull] = raw[hdr_idx].copy().view("<i4").reshape(-1, 2)
    n_parts = hdr[:, 0].astype(np.int64)
    n_points = hdr[:, 1].astype(np.int64)

    total_points = int(n_points.sum())
    xy = np.empty((total_points, 2), dtype=np.float64)

    part_offsets_list = []
    part_record_list = []
    cursor = 0
    for r in range(n_rec):
        npt = int(n_points[r])
        if npt == 0:
            continue
        npar = int(n_parts[r])
        base = int(offsets[r]) + 8 + 44  # start of the parts index array
        parts = np.frombuffer(buf, dtype="<i4", count=npar, offset=base)
        pts = np.frombuffer(
            buf, dtype="<f8", count=2 * npt, offset=base + 4 * npar
        ).reshape(npt, 2)
        xy[cursor : cursor + npt] = pts
        bounds = np.concatenate([parts.astype(np.int64), [npt]]) + cursor
        part_offsets_list.append(bounds)
        part_record_list.append(np.full(npar, r, dtype=np.int64))
        cursor += npt

    if not part_offsets_list:
        return Parts(
            xy=np.empty((0, 2)),
            part_offset=np.zeros(1, np.int64),
            part_record=np.empty(0, np.int64),
            n_records=n_rec,
        )

    # Each record contributed numParts+1 boundaries; the last of one record and
    # the first of the next coincide, so stitch them into one monotone array.
    starts = np.concatenate([b[:-1] for b in part_offsets_list])
    part_offset = np.concatenate([starts, [cursor]]).astype(np.int64)
    part_record = np.concatenate(part_record_list)
    return Parts(
        xy=xy, part_offset=part_offset, part_record=part_record, n_records=n_rec
    )


def read_shapefile_points(shp_path):
    """Read a Point shapefile. Returns (n, 2) float64 lon/lat."""
    shp_path = Path(shp_path)
    raw = np.fromfile(shp_path, dtype=np.uint8)
    buf = raw.data
    file_type = int(np.frombuffer(raw[32:36].tobytes(), dtype="<i4")[0])
    if file_type not in POINT_TYPES:
        raise ValueError(
            f"{shp_path.name} has shape type {file_type}; expected a point layer"
        )
    shx = shp_path.with_suffix(".shx")
    offsets = _read_shx_offsets(shx) if shx.exists() else _scan_offsets(raw)
    out = np.full((offsets.size, 2), np.nan)
    for r, off in enumerate(offsets):
        st = int(np.frombuffer(buf, dtype="<i4", count=1, offset=int(off) + 8)[0])
        if st == NULL:
            continue
        out[r] = np.frombuffer(buf, dtype="<f8", count=2, offset=int(off) + 12)
    return out


def _scan_offsets(raw: np.ndarray) -> np.ndarray:
    """Fallback when the .shx is missing: walk the record headers."""
    total = int(np.frombuffer(raw[24:28].tobytes(), dtype=">i4")[0]) * 2
    offs = []
    pos = 100
    while pos < total:
        offs.append(pos)
        clen = int(np.frombuffer(raw[pos + 4 : pos + 8].tobytes(), dtype=">i4")[0]) * 2
        pos += 8 + clen
    return np.array(offs, dtype=np.int64)


# ---------------------------------------------------------------------------
# DBF
# ---------------------------------------------------------------------------
def dbf_fields(dbf_path):
    """List of (name, type_char, length, decimals) for a .dbf."""
    with open(dbf_path, "rb") as fh:
        head = fh.read(32)
        hdr_len = struct.unpack("<H", head[8:10])[0]
        desc = fh.read(hdr_len - 32)
    fields = []
    for i in range(0, len(desc) - 1, 32):
        chunk = desc[i : i + 32]
        if len(chunk) < 32 or chunk[0] == 0x0D:
            break
        name = chunk[:11].split(b"\x00")[0].decode("latin-1").strip()
        fields.append((name, chr(chunk[11]), chunk[16], chunk[17]))
    return fields


def read_dbf(dbf_path, columns=None, encoding="utf-8"):
    """Read selected columns of a .dbf into a dict of numpy arrays.

    Only the requested columns are materialised, which matters: the road .dbf
    in this dataset is 23 MB of mostly unused OSM tags.
    """
    dbf_path = Path(dbf_path)
    with open(dbf_path, "rb") as fh:
        head = fh.read(32)
        n_rec = struct.unpack("<I", head[4:8])[0]
        hdr_len = struct.unpack("<H", head[8:10])[0]
        rec_len = struct.unpack("<H", head[10:12])[0]

    fields = dbf_fields(dbf_path)
    starts, pos = {}, 1  # byte 0 of each record is the deletion flag
    for name, typ, length, dec in fields:
        starts[name] = (pos, length, typ, dec)
        pos += length

    raw = np.fromfile(dbf_path, dtype=np.uint8, count=hdr_len + n_rec * rec_len)
    body = raw[hdr_len : hdr_len + n_rec * rec_len]
    if body.size < n_rec * rec_len:  # tolerate a missing 0x1A terminator
        n_rec = body.size // rec_len
        body = body[: n_rec * rec_len]
    table = body.reshape(n_rec, rec_len)

    wanted = list(starts) if columns is None else list(columns)
    out = {}
    for name in wanted:
        if name not in starts:
            raise KeyError(f"{name!r} not in {dbf_path.name}; have {list(starts)}")
        s, length, typ, dec = starts[name]
        block = table[:, s : s + length]
        text = [bytes(row).decode(encoding, "replace").strip() for row in block]
        if typ in "NF":
            vals = np.array([np.nan if t in ("", "*") else float(t) for t in text])
            if dec == 0 and np.all(np.isfinite(vals)) and np.all(vals == np.floor(vals)):
                # Keep integer-valued codes exact; they are often join keys.
                out[name] = np.array(text, dtype=object)
            else:
                out[name] = vals
        elif typ == "L":
            out[name] = np.array([t.upper() in ("T", "Y") for t in text])
        else:
            out[name] = np.array(text, dtype=object)
    return out, n_rec
