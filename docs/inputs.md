# Inputs, coverage and IDs

## Three independent GIS layers

| Layer | Geometry | Filename | Required attributes |
| --- | --- | --- | --- |
| Road network | PolyLine (including 3D/M variants) | `primsectertrunk_road.shp` | None used for routing |
| Ferry routes | PolyLine (including multipart) | `ferry_route_shape.shp` | None used for routing |
| Locality origins | Point (including Z/M variants) | `muncity_centroids.shp` | Unique text code and name in `.dbf` |

All spatial coordinates are interpreted as longitude and latitude in decimal
degrees, WGS84. Polyline shapefiles are read with a lightweight parser; no
GeoPandas runtime dependency is needed for the main computation.

The project can now build transport layers directly from an upstream OSM
snapshot using [the independent transport builder](transport.md), and origin
points from official localities using [the boundary builder](boundaries.md).
Each builder creates a versioned release and a small atomic JSON pointer that
the engine resolves automatically. Alternatively, the engine continues to
accept precisely named, unambiguous shapefiles in the input folder.

### Roads

The earlier implementation expected a `primsectertrunk_road` layer from an
OpenStreetMap-based Geofabrik shapefile export. This is **not the whole road
network**: its filename implies primary, secondary, tertiary and trunk roads
and the actual subset should be checked against its extract specification.
It therefore measures accessibility on the selected road classes, not on
every possible street or path.

### Ferries

The ferry-route geometry must represent usable **route connections**, not
merely coastline lines. Absence of a mapped ferry can make a real place seem
unreachable; a mapped or erroneously attached route can create an unrealistic
connection. Keep its acquisition date and filtering criteria.

### Locality reference points

The point layer must have exactly one row per desired matrix unit. The code
must be a unique, non-empty text identifier and **must not be cast to an
integer** (leading zeros and prefixes matter). The optional `config.py`
settings `CENTROID_ID_COL` and `CENTROID_NAME_COL` override field-name
autodetection. Autodetection looks for code/PSGC-like and name-like fields.

!!! warning "Historical identifiers are not automatically current PSGC codes"
    The archived output contains identifiers such as `PH0102801`.
    The PSA now publishes **10-digit PSGC codes**. Treat these as different
    code systems until a verified, time-stamped concordance establishes a
    one-to-one mapping. Changing polygons or point records does not update
    the identities or the historical distance matrix automatically.

The matrix order is **the order of rows in the point shapefile**, recorded in
`towns.csv`. Never rely on alphabetical order or array position without
loading that file. The pipeline rejects duplicate or missing point IDs.

## Shape versus administrative unit

The road and ferry graph is independent of administrative polygons. Polygons
only enter the calculation if you **choose** to generate origin points from
them beforehand. A municipality's geographic centroid may be offshore or far
from settlement centers: origin definition is an analytical assumption.

## Input audit checklist

1. Record the publication date, URL, license, and SHA-256 of every source file
   and shapefile sidecar. The engine now writes the latter automatically in
   `out/source_manifest.json`; upstream PBF and polygon sources have their own build manifests.
2. Verify that geographic coordinates are WGS84 and every polyline is correctly
   noded into intended connections.
3. Audit major ferry corridors and route continuity, especially across islands.
4. Check exact ID coverage against the time-consistent administrative roster.
5. Inspect unusually large `snap_offset_km` values and `snap_tier` in the
   output; they can identify severe geometry mismatches.

See [Reproducibility](reproducibility.md) for sources and publication guidance.
