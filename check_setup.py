"""Beginner-friendly preflight check for the distance-matrix project."""

from __future__ import annotations

import struct
import argparse
from pathlib import Path

import config as C
from net_distance_ph.shpio import dbf_fields
from run import _locate

EXPECTED = {
    C.ROAD_SHP: {3, 13, 23},
    C.FERRY_SHP: {3, 13, 23},
    C.CENTROID_SHP: {1, 11, 21},
}


def shp_header(path: Path) -> tuple[int, tuple[float, float, float, float]]:
    with path.open("rb") as fh:
        head = fh.read(100)
    if len(head) < 100:
        raise ValueError("file is shorter than the 100-byte shapefile header")
    shape_type = struct.unpack("<i", head[32:36])[0]
    bbox = struct.unpack("<4d", head[36:68])
    return shape_type, bbox


def find_exact(filename: str) -> Path:
    """Resolve selected versioned releases or unambiguous legacy inputs.

    Uses run._locate, which raises an ordinary exception, so a missing or
    ambiguous file is reported as FAIL and the remaining files are still
    checked (run._find would stop the program at the first problem).
    """
    return _locate(filename)


def bbox_looks_lonlat(bbox: tuple[float, float, float, float]) -> bool:
    xmin, ymin, xmax, ymax = bbox
    return (
        -180 <= xmin <= 180
        and -180 <= xmax <= 180
        and -90 <= ymin <= 90
        and -90 <= ymax <= 90
        and xmin <= xmax
        and ymin <= ymax
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="directory containing input shapefiles")
    args = parser.parse_args()
    if args.data is not None:
        C.DATA = args.data.expanduser().resolve()

    print("Philippine distance-matrix setup check")
    print(f"Data folder: {Path(C.DATA)}")
    print(f"Project folder: {Path(C.PROJECT_DIR)}")
    print()

    if not Path(C.DATA).is_dir():
        print("FAIL: the data folder does not exist.")
        print("Set NET_DISTANCE_PH_DATA or pass --data with the folder location.")
        return 1

    failed = False
    found: dict[str, Path] = {}
    for filename, allowed_types in EXPECTED.items():
        try:
            path = find_exact(filename)
            found[filename] = path
            shape_type, bbox = shp_header(path)
            type_ok = shape_type in allowed_types
            coord_ok = bbox_looks_lonlat(bbox)
            print(f"FOUND: {filename}")
            print(f"  {path}")
            print(f"  shape type: {shape_type} ({'OK' if type_ok else 'WRONG TYPE'})")
            print(
                "  bounding box: "
                f"x={bbox[0]:.6f}..{bbox[2]:.6f}, y={bbox[1]:.6f}..{bbox[3]:.6f} "
                f"({'looks like longitude/latitude' if coord_ok else 'NOT longitude/latitude'})"
            )
            if not type_ok or not coord_ok:
                failed = True

            shx = path.with_suffix(".shx")
            print(f"  .shx sidecar: {'FOUND' if shx.exists() else 'missing (reader can scan without it)'}")
            if filename == C.CENTROID_SHP:
                dbf = path.with_suffix(".dbf")
                if not dbf.exists():
                    print("  .dbf sidecar: MISSING — required for PSGC code and name")
                    failed = True
                else:
                    fields = [name for name, *_ in dbf_fields(dbf)]
                    print(f"  .dbf sidecar: FOUND; fields: {', '.join(fields)}")
            print()
        except Exception as exc:
            print(f"FAIL: {filename}: {exc}")
            print()
            failed = True

    if failed:
        print("SETUP CHECK FAILED. Fix the items marked FAIL/WRONG before running run.py.")
        return 1

    print("SETUP CHECK PASSED.")
    data_flag = f' --data "{Path(C.DATA)}"' if args.data is not None else ""
    print(f"Next command: python run.py all{data_flag} --workers 4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
