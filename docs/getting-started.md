# Getting started

<!-- v55-methods-documentation -->

## Primary: population-weighted town distance matrix

The primary research command is `net-distance-ph population-weighted`, which defaults to **all three matrices (town, province-equivalent and region)** and **balanced connected-component-aware network k-medians**. It needs a compatible stage-1 graph and chosen barangay geometry in the workspace. From an existing previously completed Faeldon run:

```powershell
net-distance-ph population-weighted
# Equivalently, pin existing Faeldon points explicitly:
net-distance-ph population-weighted --geometry-source faeldon --no-auto-geometry
# Optional only one matrix:
net-distance-ph population-weighted --level town
```

A bare invocation first reuses the **exact SHA-256-verified geometry from the latest completed population run**. With no earlier run, it uses the original OCHA-first-run policy. For a first build, prepare the road/ferry graph with `net-distance-ph run --until graph` and ensure the selected population geometry source is available; do not disable automatic geometry acquisition unless it is already cached. See [the primary method](population_weighted.md) for precise inputs, limitations, and formulas.

## View and compare completed output runs

After at least one population-weighted run has finished, install the viewer's
GIS dependencies (if not already installed) and open the local explorer:

```powershell
python -m pip install -e ".[test,geo,transport]"
net-distance-ph view
```

The browser opens automatically at `http://127.0.0.1:<port>/`. Keep the
terminal running while you explore; **Ctrl+C** stops the local server. Use
the **Completed run** and **Geographic level** selections to switch between
available dated output folders and town, province-equivalent, or region
matrices. Click a polygon or search by name/PSGC code, then set **Closest X**
to highlight and rank finite-distance neighbors. You can toggle boundary,
road-skeleton and ferry layers and the light/dark theme.

For a nondefault workspace, or to open the printed URL manually:

```powershell
net-distance-ph view --workspace "D:\Research\net-distance-ph" --port 8765 --no-open
```

This is a **read-only local viewer**, not a GitHub Pages data service. It reads
existing matrices and prepares reusable simplified display layers under
`variants/population_weighted/view_cache/`; no new routing computation is
performed. The displayed geography uses `current_boundaries.json` and
`current_transport.json`, so an old matrix may be shown with a newer map
release. See [the CLI reference](cli.md#local-output-explorer-view) for details.

## Legacy centroid pipeline setup

The commands in the following sections describe the separate `run.py` and `net-distance-ph run` baseline pipeline. Its `out/` files are **not** the population-weighted OD output.


## Requirements

Python 3.11+ with NumPy and SciPy. `pyproj` is recommended for geodesic edge
lengths; if absent, a built-in ellipsoidal approximation is used. The optional
polygon conversion script requires GeoPandas and its dependencies.

```bash
# Clone (after publishing your repository)
git clone "https://github.com/miraflor/net-distance-ph.git"
cd net-distance-ph

# Install and verify
python -m pip install -e ".[test]"
python -m pytest -q
```

Dependencies are specified in
`pyproject.toml`. On Windows, PowerShell accepts the same quoted pip extras
and commands.

## Legacy centroid automatic source preparation

The PSA Q2 2026 workbook and derived JSON are stored in the repository
under `reference/psgc/`. The boundary builder reads that JSON by default;
a live PSA download is **not** required. geoBoundaries and OSM still
require a network connection on the first build.

Install the optional source-builder requirements and start the full workflow:

```powershell
python -m pip install -e ".[test,geo,transport,docs]"
python run.py all --workers 4 --verbose
```

Without `--data` or `NET_DISTANCE_PH_DATA`, `all` and `graph` first generate
missing municipality origins from PSA PSGC and geoBoundaries, and road/ferry
layers from the Geofabrik Philippines OSM extract. Input snapshots, versioned
releases, checkpoints, computed matrices and timestamped logs live in:

```text
~/Documents/net-distance-ph/
  data/      # upstream sources, audit, versioned selected releases
  work/      # resumable graph and matrix checkpoints
  output/    # CSV/NPY matrices, reports and verification
  logs/      # run-YYYYMMDD-HHMMSS-PID.log
```

`python run.py prepare --verbose` runs *only* the input builders. Complete
selected releases are reused on subsequent runs; they are not silently
refreshed or replaced. `--workspace PATH` changes the root; explicit `--data`
(or `NET_DISTANCE_PH_DATA`) disables automatic acquisition for `all`/`graph`
so existing custom input workflows remain supported. `--no-auto-prepare`
also disables automatic acquisition. The `--heartbeat-seconds N` option
controls elapsed-time liveness messages; all console lines are timestamped and
mirrored to the session log. Even during long operations, heartbeat messages
show that the process is running without inventing numeric progress.

**Data-vintage caution:** the default PSA Q2 2026 locality roster and the
older geoBoundaries polygons are not guaranteed to reconcile automatically.
If a boundary build stops, review `data/boundary_build_incomplete/` inside the
workspace and use the explicit builder overrides described in
[Boundary generation](boundaries.md). The workflow intentionally does not
assume or fabricate polygon matches. National-scale build completion and
full routing network quality have not yet been independently verified.

## Prepare files (existing shapefiles)

You can [construct origin points from administrative boundaries](boundaries.md)
and [extract roads/ferries from OpenStreetMap](transport.md) directly, or use
previously prepared and verified shapefiles.

For independently prepared shapefiles, use these canonical, role-based
filenames in any subdirectories under a single root. The three historical
basenames remain accepted for backward compatibility:

```text
external-gis-data/
├── roads/
│   ├── roads.shp
│   ├── roads.shx
│   └── roads.dbf
├── ferry_routes/
│   └── ferry_routes.shp
└── locality_origins/
    ├── locality_origins.shp
    └── locality_origins.dbf
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
to each relevant command or set the `NET_DISTANCE_PH_WORK` and
`NET_DISTANCE_PH_OUT` environment variables persistently.

With explicit `--data`, source acquisition is opt-in; the default workspace-first
`all` and `graph` commands automatically invoke the source builders if needed.
Stage 1 can require significant memory;
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

The completed command prints **OUTPUT [TOWN]**, **OUTPUT [PROVINCE_EQUIVALENT]**, and **OUTPUT [REGION]** with absolute paths and the location of `latest_run.json`. All three share the same graph, k-medians and routing calculation.
