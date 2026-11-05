# Getting started

## Requirements

Python 3.11+ with NumPy and SciPy. `pyproj` is recommended for geodesic edge
lengths; if absent, a built-in ellipsoidal approximation is used. The optional
polygon conversion script requires GeoPandas and its dependencies.

```bash
# Clone (after publishing your repository)
git clone "https://github.com/miraflor/phnetdist.git"
cd phnetdist

# Install and verify
python -m pip install -e ".[test]"
python -m pytest -q
```

Dependencies are specified in
`pyproject.toml`. On Windows, PowerShell accepts the same quoted pip extras
and commands.

## Prepare files

You can [construct origin points from administrative boundaries](boundaries.md)
and [extract roads/ferries from OpenStreetMap](transport.md) directly, or use
previously prepared and verified shapefiles.

For independently prepared shapefiles, use the expected three filenames, in
any subdirectories under a single root:

```text
external-gis-data/
├── primsectertrunk_road_shp/
│   ├── primsectertrunk_road.shp
│   ├── primsectertrunk_road.shx
│   └── primsectertrunk_road.dbf
├── ferry_route_shape_shp/
│   └── ferry_route_shape.shp
└── muncity_centroids_shp/
    ├── muncity_centroids.shp
    └── muncity_centroids.dbf
```

Input spatial coordinates **must already be WGS84 longitude/latitude**
(EPSG:4326); this pipeline does not automatically reproject the road or ferry
layers. The preflight checks bounding boxes but cannot prove a CRS from numbers
alone. Validate source provenance and CRS in a GIS tool first.

## Preflight

```bash
python check_setup.py --data "/absolute/path/to/external-gis-data"
```

This checks shapefile presence, geometry types, geographic-looking bounding
boxes, and centroid attributes. Duplicate matching filenames are now rejected,
not silently selected. Resolve duplicates or narrow the `--data` path.

## Run

```bash
# One command for all four stages
python run.py all --data "/absolute/path/to/external-gis-data" --workers 4

# Or run one stage at a time
python run.py graph --data "/absolute/path/to/external-gis-data"
python run.py reduce --data "/absolute/path/to/external-gis-data"
python run.py matrix --data "/absolute/path/to/external-gis-data" --workers 4
python run.py verify --data "/absolute/path/to/external-gis-data"
```

To store outputs elsewhere, add `--work /path/to/work --out /path/to/out`
to each relevant command or set the `PHNETDIST_WORK` and
`PHNETDIST_OUT` environment variables persistently.

No automatic download occurs inside the shortest-path runner; instead,
`build_boundaries.py` and `build_transport.py` fetch sources only when explicitly
invoked. Stage 1 can require significant memory;
the reduction stage is CPU-intensive and saves resumable checkpoints.

## Optional: generate centroids from boundaries

```bash
python -m pip install -e ".[geo]"
python scripts/centroids_from_boundaries.py --help
```

For independent PSA and geoBoundaries source acquisition and centroid generation, see [boundary generation](boundaries.md).
The converter is **opt-in**; existing centroid input remains the default.

## Try it without national GIS data

```bash
python -m pytest tests/test_end_to_end.py -q
```

That test generates a miniature synthetic archipelago with road grids,
offshore ferry endpoints and an isolated island, executes the full CLI, and
compares its matrix against unreduced Dijkstra results. It does not access any separate GIS project
or download any source data.
