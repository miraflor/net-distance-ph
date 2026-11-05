#!/usr/bin/env python3
"""Make a WGS84 centroid shapefile from a local municipality/city polygon layer.

Optional extra: ``python -m pip install -e '.[geo]'``

This prepares *input points only*: it neither downloads national data nor
changes road/ferry routing. See docs/boundaries.md for assumptions and cautions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundaries", required=True, type=Path,
                        help="local boundary .gpkg, .shp, or other GeoPandas-readable file")
    parser.add_argument("--layer", default=None, help="GPKG layer name, if required")
    parser.add_argument("--output", required=True, type=Path,
                        help="output WGS84 point .shp; basename used by run.py")
    parser.add_argument("--code-column", default="psgc_code",
                        help="stable *text* municipality/city identifier (default psgc_code)")
    parser.add_argument("--name-column", default="name",
                        help="municipality/city label field (default name)")
    parser.add_argument("--kind-column", default=None,
                        help="optional column to filter municipality/city features")
    parser.add_argument("--kind-values", default="city,municipality",
                        help="comma-separated eligible values in --kind-column")
    parser.add_argument("--method", choices=["centroid", "representative-point"],
                        default="centroid",
                        help="default: equal-area polygon centroid (can be offshore)")
    parser.add_argument("--expected-count", type=int, default=None,
                        help="optional fail-fast expected city/municipality count")
    return parser.parse_args()


def convert(args: argparse.Namespace) -> dict:
    """Build points without altering the underlying boundaries or place IDs."""
    try:
        import geopandas as gpd
        import numpy as np
    except ImportError as exc:
        raise SystemExit("Install the optional GIS dependency: pip install -e '.[geo]'") from exc

    source = args.boundaries.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Boundary file not found: {source}")
    if args.output.suffix.lower() != ".shp":
        raise ValueError("--output must end in .shp")
    if source == args.output.resolve():
        raise ValueError("The output cannot overwrite the input boundary file")

    options = {"layer": args.layer} if args.layer else {}
    polygons = gpd.read_file(source, **options)
    if polygons.crs is None:
        raise ValueError("Boundaries have no declared CRS; repair the source CRS first")
    missing = {args.code_column, args.name_column} - set(polygons.columns)
    if missing:
        raise ValueError(f"Missing columns {sorted(missing)}. Available: {list(polygons.columns)}")

    if args.kind_column:
        if args.kind_column not in polygons:
            raise ValueError(f"Missing kind column: {args.kind_column}")
        allowed = {token.strip().casefold() for token in args.kind_values.split(",") if token.strip()}
        polygons = polygons.loc[
            polygons[args.kind_column].astype(str).str.strip().str.casefold().isin(allowed)
        ].copy()
    if polygons.empty:
        raise ValueError("No municipality/city polygons after filtering")
    if not polygons.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("Expected only Polygon or MultiPolygon geometries; filter out non-LGU areas")
    if polygons.geometry.isna().any() or polygons.geometry.is_empty.any():
        raise ValueError("At least one boundary has an empty geometry")
    if not polygons.geometry.is_valid.all():
        raise ValueError("At least one boundary is invalid; repair or audit geometry upstream")

    # Test for missing values *before* converting to text: with pandas 2.x,
    # astype(str) turns a missing value into the strings "None" or "nan",
    # which would then pass as a valid code.
    if polygons[args.code_column].isna().any() or polygons[args.name_column].isna().any():
        raise ValueError("All codes and names must be nonempty (missing values found)")
    codes = polygons[args.code_column].astype(str).str.strip()
    names = polygons[args.name_column].astype(str).str.strip()
    if codes.eq("").any() or names.eq("").any():
        raise ValueError("All codes and names must be nonempty")
    if codes.duplicated().any():
        duplicates = codes[codes.duplicated()].tolist()[:10]
        raise ValueError(f"Duplicate locality codes: {duplicates}")
    if args.expected_count is not None and len(polygons) != args.expected_count:
        raise ValueError(f"Expected {args.expected_count} localities; found {len(polygons)}")

    # Equal-area projection avoids the more severe lat/lon degree-centroid
    # distortion; this is a geographic representative location, *not* a
    # population centroid or municipal hall location.
    equal_area = polygons.to_crs("EPSG:6933")
    centers = (equal_area.geometry.centroid if args.method == "centroid"
               else equal_area.geometry.representative_point())
    points = gpd.GeoSeries(centers, crs="EPSG:6933").to_crs("EPSG:4326")
    if not np.isfinite(points.x).all() or not np.isfinite(points.y).all():
        raise ValueError("Generated nonfinite coordinates")
    if (points.x.abs() > 180).any() or (points.y.abs() > 90).any():
        raise ValueError("Generated coordinates outside WGS84 longitude/latitude")

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # DBF has a 10-character field name limit; these names are recognized by
    # the existing auto-detection in run.py without changing its semantics.
    attrs = gpd.GeoDataFrame(
        {"PSGC_CODE": codes.to_numpy(), "ADM3_EN": names.to_numpy()},
        geometry=points.to_numpy(), crs="EPSG:4326",
    )
    attrs.to_file(output, driver="ESRI Shapefile", index=False, encoding="UTF-8")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    manifest = {
        "source_file": str(source),
        "source_sha256": digest.hexdigest(),
        "source_layer": args.layer,
        "input_crs": str(polygons.crs),
        "centroid_crs": "EPSG:6933",
        "output_crs": "EPSG:4326",
        "method": args.method,
        "code_column": args.code_column,
        "name_column": args.name_column,
        "kind_column": args.kind_column,
        "kind_values": args.kind_values if args.kind_column else None,
        "count": len(attrs),
        "output": str(output),
        "interpretation": "geometric polygon centroid" if args.method == "centroid" else "point on polygon surface",
    }
    output.with_suffix(".provenance.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    result = convert(_arguments())
    print(f"Prepared {result['count']:,} city/municipality points: {result['output']}")
    print("Provenance recorded beside the shapefile.")


if __name__ == "__main__":
    main()
