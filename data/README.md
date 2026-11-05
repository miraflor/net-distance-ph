# Local GIS inputs (not committed)

Place the following three WGS84 shapefiles here, optionally in subfolders:

- `primsectertrunk_road.shp` (+ `.shx`, `.dbf` when supplied)
- `ferry_route_shape.shp` (+ `.shx`, `.dbf` when supplied)
- `muncity_centroids.shp` (+ `.dbf` holding unique stable text identifiers and names)

See [Input data](../docs/inputs.md) and [Reusing administrative boundaries](../docs/boundaries.md).
The original dataset and generated matrices are deliberately excluded from git.

Generated upstream source files and versioned releases are ignored by git.
The two optional builders write `current_boundaries.json` and
`current_transport.json`, each pointing to an immutable release directory.
Never commit a large raw PBF, a generated national shapefile, or a matrix
without checking provenance and applicable data licenses.
