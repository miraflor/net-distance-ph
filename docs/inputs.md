# Inputs, coverage and IDs

## Three independent GIS layers

| Layer | Geometry | Filename | Required attributes |
| --- | --- | --- | --- |
| Road network | PolyLine (including 3D/M variants) | `roads.shp` (new releases) | None used for routing |
| Ferry routes | PolyLine (including multipart) | `ferry_routes.shp` (new releases) | None used for routing |
| Locality origins | Point (including Z/M variants) | `locality_origins.shp` (new releases) | Unique text code and name in `.dbf` |

All spatial coordinates are interpreted as longitude and latitude in decimal
degrees, WGS84. Polyline shapefiles are read with a lightweight parser; no
GeoPandas runtime dependency is needed for the main computation.

The filenames above are class- and boundary-level-neutral. Older selected
releases retain `primsectertrunk_road.shp`, `ferry_route_shape.shp`, and
`muncity_centroids.shp` and remain compatible. See [source file naming](source_names.md).

The project can now build transport layers directly from an upstream OSM
snapshot using [the independent transport builder](transport.md), and origin
points from official localities using [the boundary builder](boundaries.md).
Each builder creates a versioned release and a small atomic JSON pointer that
the engine resolves automatically. Alternatively, the engine continues to
accept precisely named, unambiguous shapefiles in the input folder.

### Roads

The original `primsectertrunk_road.shp` filename remains for compatibility,
but it **no longer identifies the selected highway classes**. The default
`comprehensive` OSM extraction now includes 14 motor-road classes, including
NLEX/SLEX motorway ramps, residential and public service roads. The previous
four-class selection is available as `--road-profile major`. Read the exact
`road_filter` in `current_transport.json`'s selected release provenance and
consult [road profiles](road_profiles.md). The graph does not yet enforce
one-way or turn restrictions or all conditional road-access permissions.

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

!!! warning "Use a single administrative code vintage"
    The Q2 2026 PSGC roster uses **ten-digit text identifiers**. If supplied point data use other codes, require an independently verified crosswalk. A changed roster or set of points requires a new matrix.

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
