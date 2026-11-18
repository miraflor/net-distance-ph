# net-distance-ph

<!-- v55-methods-documentation -->

For a PSGC-aligned replacement to the older geoBoundaries ADM3 polygons, use `net-distance-ph boundaries --download` to validate the pinned 1,642-locality source, then `net-distance-ph boundaries --download --publish` to explicitly publish one Manila city polygon and the eight SGA municipalities. See [PSGC-aligned boundaries](docs/psgc_aligned_boundaries.md).

**Two reproducible OD workflows: primary population-weighted town-to-town distance and a separate legacy locality-centroid matrix.**

The project builds an undirected graph from independently prepared or
user-supplied OpenStreetMap road and ferry-route shapefiles, attaches locality reference points, contracts the
network without changing shortest-path distances, and computes a square matrix
in kilometres. It is designed for national-scale runs with resumable stages,
clear row/column identifiers, and numerical verification against the corresponding unreduced graph.

**National-run status:** Maintainer-local national population-weighted runs and geography checks have been reported; national matrices and source snapshots are not distributed with this repository or independently certified as real-world ferry accessibility. See [Run status](docs/run-status.md).

**[Documentation (GitHub Pages after deployment)](https://miraflor.github.io/net-distance-ph/)** ·
[Methodology](docs/methodology.md) · [Inputs](docs/inputs.md) ·
[Independent boundary generation](docs/boundaries.md) ·
[Independent transport extraction](docs/transport.md) · [Validation](docs/validation.md)

## Primary workflow: population-weighted town-to-town OD

**The primary research command is `net-distance-ph population-weighted`.** The default now exports **town, province-equivalent, and region** (`--level all`) in one run, with **balanced, connected-component-aware network k-medians** (`--median-selection balanced`). Upper tiers aggregate the same town-routing totals, **not** separate graph constructions.

```powershell
# The default exports all three matrices and reuses exact SHA-256-verified geometry
# from the last completed run, if one exists:
net-distance-ph population-weighted

# To pin the known Faeldon source explicitly (no geometry download):
net-distance-ph population-weighted --geometry-source faeldon --no-auto-geometry

# Or request only one output tier:
net-distance-ph population-weighted --level town
```

Without an explicit geometry choice, an existing completed run supplies its **exact hash-verified polygon geometry**; otherwise a fresh run retains the historical OCHA source policy. `--geometry-source faeldon|ocha|auto` overrides that reuse rule. The population inputs are 2024 barangay counts linked to the Q2 2026 PSGC hierarchy. Each positive-population barangay contributes one interior polygon representative point, which is snapped to the existing road/ferry network. Disconnected graph components are **preserved**: at least one weighted network median is chosen per populated component, and extra medoids are allocated by a deterministic, **project-defined** normalized access/complexity criterion (subject to the per-town cap). Site-selection heuristics do not guarantee global k-median optimality.

The returned **distance** is the population-pair-weighted mean of **reachable selected representative-node shortest paths**, conditional on a modeled connection. It **does not include** the origin-to-network snap or the origin-to-median representation-access distance, which are recorded separately. The companion reachability matrix divides modeled reachable population-pair mass by the **full** census population-pair mass, including unmodeled origins. An `inf` distance means no modeled representative-site pair was demonstrably reachable, **not** necessarily that travel is impossible in the real world; within-unit diagonals need not be zero.

`--level province` produces a **province-equivalent** matrix (`province_equivalent/`): each published province plus each city or municipality without a province parent as a peer OD unit. This exhaustively groups towns without guessing HUC versus ICC legal labels from absent parent fields. The **same barangay populations** underlie all levels; aggregation sums unnormalized reachable-pair totals before taking a ratio. Old `province/` outputs are left untouched.

Each completed invocation publishes its matrices under a **local-time-stamped output folder** such as `output/2026-11-18_19-20-30/` (sortable `YYYY-MM-DD_HH-MM-SS`, no spaces); the CLI prints the full paths. Computation caches retain their hash-based identifiers, and earlier hash-named output runs remain readable.

**Read before using the matrices:** [complete population-weighted methodology](docs/population_weighted.md), [output semantics](docs/outputs.md), [foundational literature and novel choices](docs/literature.md), [validation](docs/validation.md), and [run status](docs/run-status.md). The legacy centroid pipeline, described below, has **a different distance definition** and remains available separately.

## Explore completed outputs

The **local Net Distance PH Explorer** opens completed population-weighted runs in
an interactive map. It automatically discovers the output folders in the
workspace and lets you switch between available **town, province-equivalent,
and region** matrices without selecting `.npy` files manually.

```powershell
# The viewer uses the existing workspace; install the GIS dependencies once.
python -m pip install -e ".[test,geo,transport]"
net-distance-ph view

# Optional: an alternate workspace and a fixed local port, without opening a tab.
net-distance-ph view --workspace "D:\Research\net-distance-ph" --port 8765 --no-open
```

The command serves a **read-only local webpage** at `http://127.0.0.1:<port>/`
(the port is chosen automatically unless specified). Keep the terminal open
while using the page and press **Ctrl+C** to stop it. This is **not** a public
GitHub Pages service and does not upload your matrices.

Select a completed run and geographic level, click or search for a locality,
and highlight its **closest X reachable units**, ranked by the stored
conditional population-weighted distance. The interface also shows modeled
reachability, supports light/dark mode, and offers optional administrative
polygons, a simplified road skeleton, and ferry-route overlays.

The viewer reads the existing `distance_matrix_km.npy`,
`reachable_pair_probability.npy`, and `units.csv` without rerunning shortest
paths. Display-only map geometry is prepared and reused under
`variants/population_weighted/view_cache/`; the original GeoPackage,
Shapefiles, and matrices are unchanged. Map layers come from the **currently
selected** boundary and transport releases, which may differ from those of a
historical output run. The overlays are context, **not reconstructed OD paths**.
See [the CLI explorer reference](docs/cli.md#local-output-explorer-view)
for options and limitations.

## Inspect and audit existing results

The installed CLI also provides read-only checks and targeted diagnostics.
These commands operate on the current workspace, but each audit has its own
prerequisites; consult the [CLI reference](docs/cli.md) before running one.

| Command | Purpose |
| --- | --- |
| `net-distance-ph status` / `doctor` | Inspect available files or check dependencies and source headers; neither validates nationwide geographic accuracy |
| `net-distance-ph observe` | Summarize recorded run timing and evidence from existing logs |
| `net-distance-ph audit-geography` | Audit population coverage, province assignments, and modeled reachability |
| `net-distance-ph audit-topology` | Trace disconnected representative-site components in the saved graph |
| `net-distance-ph inspect-topology-osm` | Investigate potential topology gaps against the cached original OSM data |
| `net-distance-ph audit-origin-snap` | Examine population-origin snapping sensitivity |
| `net-distance-ph audit-polygon-network` | Check selected polygon and road-network consistency cases |
| `net-distance-ph reset` | **Preview only** which computational files would be removed; deletion needs explicit confirmation and `--execute` |

These diagnostics do not establish current ferry operations or legally
traversable routes, and the viewer does not change the model.

## Legacy centroid quick start

The installed pipeline now **automatically validates and selects** pinned PSGC-aligned
municipal boundaries on the first run; no separate `boundaries --publish` command is required:

```powershell
net-distance-ph run --until connectivity --verbose
# Optional levels (isolated workspaces):
net-distance-ph run --until connectivity --boundary-level city --verbose
net-distance-ph run --until connectivity --boundary-level province --verbose
```

See [Boundary levels and source limitations](docs/boundary_levels.md).

The installed CLI is **`net-distance-ph`** (see [CLI reference](docs/cli.md)). Run `net-distance-ph status` to inspect the workspace, `net-distance-ph doctor` for source preflight, and `net-distance-ph run --until connectivity` for the first national connectivity experiment.


Python 3.11 or newer; run from the repository root:

```bash
python -m pip install -e ".[test,geo,transport,docs]"
python -m pytest -q
# Automatically acquires missing PSA/geoBoundaries + OSM inputs into Documents
net-distance-ph run --until connectivity --verbose
# Or prepare sources only (no matrix computation): python run.py prepare --verbose
```

By default, a persistent workspace is created at
`~/Documents/net-distance-ph/` with `data/` (source cache and releases),
`work/` (graph and checkpoints), `output/` (matrices), and `logs/` (timestamped
run logs). Existing valid source releases are reused. A first national build
requires internet and can halt for manual reconciliation of unmatched 2026 PSGC
and older geoBoundaries records; it will not silently invent matches.

Use `--workspace PATH` to select another workspace, `--no-auto-prepare` to
require pre-existing sources, and `--heartbeat-seconds N` to change the
30-second liveness updates. `python run.py prepare` only prepares the sources.
All stage messages are timestamped in the console and the session log.

### Existing shapefiles (advanced)

The input directory must contain these WGS84 shapefiles (subfolders are allowed):

| Input | Required basename | Purpose |
| --- | --- | --- |
| Road polylines | `roads.shp` | Road network edges |
| Ferry polylines | `ferry_routes.shp` | Sea connections |
| Locality points | `locality_origins.shp` | Matrix origin/destination points, text IDs and labels |

The original three basenames remain valid for legacy/manual sources, and
published release pointers resolve both naming schemes. See
[source naming](docs/source_names.md).

Configure input, work and output locations using `--data`, `--work`, `--out`,
or the `NET_DISTANCE_PH_DATA`, `NET_DISTANCE_PH_WORK`, and
`NET_DISTANCE_PH_OUT` environment variables. Default paths are `data/`,
`work/`, and `out/` under the project root. The three source filenames,
parameters and stages remain configurable in `config.py`.

```bash
python run.py graph --data "/path/to/shapefiles"
python run.py reduce --data "/path/to/shapefiles"                 # resumable contraction checkpoint
python run.py matrix --data "/path/to/shapefiles" --workers 4    # resumable matrix rows/waves
python run.py verify --data "/path/to/shapefiles"                # compares with unreduced graph
```

## Connectivity preflight (before the distance matrix)

The connectivity audit is **not** a national result until you run it on your
own completed stage-1 graph. It checks the 1,642 Q2 2026 official PSGC towns,
snapping, and road-only versus combined road/ferry components. It does **not**
calculate all-pairs shortest-path distances or automatically download inputs.

```powershell
python -m pytest tests/test_connectivity.py tests/test_connectivity_cli.py -q
python run.py graph --verbose            # prepares missing sources if needed
python run.py connectivity --verbose     # only analyzes stage-1 outputs
```

Alternatively, after `python -m pip install -e .`, run
`net-distance-ph-connectivity` or `python -m net_distance_ph.connectivity`.
Outputs go into the workspace's `output/connectivity_audit/` directory.
A nonzero exit status indicates unproven connectivity or missing inputs;
see [connectivity guidance](docs/connectivity.md).

## Legacy centroid matrix outputs

`out/distance_matrix_km.npy` and `.csv` contain the distances (kilometres);
`out/towns.csv` fixes the row/column order. `out/row_validity_radius_km.npy`
distinguishes incomplete rows from complete rows containing unreachable
pairs. `out/matrix_report.json` and `out/verification.txt` summarize results.

An `inf` entry in the NumPy matrix is **not always an unreachable route**:
during a partial run, it may be a pair beyond that row's computed radius.
CSV blanks likewise do not distinguish these two cases. See the
[output contract](docs/outputs.md).

## Acquire source data independently

The official PSA PSGC Q2 2026 workbook is **pinned in** [`reference/psgc/`](reference/psgc/README.md),
with a complete compressed JSON Lines conversion and a validated 1,642-locality JSON roster.
The boundary builder uses the JSON roster automatically (no PSA HTTP request); run
`python scripts/convert_psgc.py --check` to verify it against the original XLSX.
geoBoundaries polygons and OSM road/ferry extracts are still acquired separately.

The default `python run.py all` automatically invokes the two independent source builders when inputs are missing. They also remain available as standalone tools and acquire upstream
public geographic sources directly, without imports from any other research project.

**A. City and municipality boundaries and origin points** (PSA PSGC + geoBoundaries):

```bash
python -m pip install -e ".[geo]"
python scripts/build_boundaries.py --output data
```

Vintage differences are real: the older ADM3 geometry and the Q2 2026 PSGC
roster are not guaranteed to match one-to-one. The script audits all candidates
and refuses to publish an incomplete reconciliation. A reviewed override
crosswalk may be needed. It produces a versioned GeoPackage, locality points,
matching audit, and source provenance. [Boundary methodology](docs/boundaries.md).

**B. Roads and ferries** (OpenStreetMap from Geofabrik):

```bash
python -m pip install -e ".[transport]"
python scripts/build_transport.py --download --output data
```

This deliberately downloads the Philippines OSM extract and extracts selected
highway classes and ferry ways, including those referenced only through ferry
route relations. The PBF remains cached and content-hashed. The new releases
are selected through atomic `data/current_boundaries.json` and
`data/current_transport.json` files; older releases remain intact.
[Transport methodology and limitations](docs/transport.md).

Once both inputs are ready:

```bash
python check_setup.py --data data
python run.py all --data data --workers 4
```

These source-building steps are optional. Existing audited shapefiles remain
supported. A live national download, boundary reconciliation, and nationwide
ferry/network audit are **not yet verified** by the offline synthetic tests.

## Scope and safeguards

- **Implemented:** road/ferry network construction, contraction, neighbor-wave Dijkstra and verification against the unreduced graph. Synthetic tests do **not** establish nationwide correctness or connectivity.
- **Improved:** portable paths, atomic release pointers, SHA-256 input validation across all stages, stable text-ID validation, stale-checkpoint prevention, prompt Ctrl-C, effective numerical verification, independent boundary and transport extraction, automated regression tests, and GitHub Actions. See the [changelog](CHANGELOG.md).
- **Not included:** private local paths, national input shapefiles, intermediate graph arrays, or previously calculated matrices. Do not publish datasets without reviewing licensing and provenance.
- **Model assumptions:** undirected roads/ferries, geometry-based path lengths, optional centroid connector distance, and optional per-ferry-segment cost adjustment. These are **not** observed travel times or a turn-restricted transportation model.

## Publishing the documentation

A MkDocs Material site is included in `docs/`, configured in `mkdocs.yml`.
The [GitHub Pages workflow](.github/workflows/pages.yml) builds and deploys it
on a push to `main` after Pages is configured to use **GitHub Actions**.
For local preview:

```bash
python -m pip install -e ".[docs]"
python -m mkdocs serve
```

## Author and Citation

**James Matthew Miraflor**  
[Scientific Computing Laboratory](https://scl.dcs.upd.edu.ph/)  
[Department of Computer Science](https://dcs.upd.edu.ph/)  
College of Engineering  
University of the Philippines Diliman

**net-distance-ph** is authored and maintained by James Matthew Miraflor, with AI assistance in software development, debugging, testing, and documentation. Responsibility for the project's methodological decisions, scientific validity, and published outputs rests with the author.

### Citing this software

If you use **net-distance-ph** in academic research, please cite:

Miraflor, J. M. (2026). *net-distance-ph: Population-weighted road–ferry network distance matrices for the Philippines* [Computer software]. GitHub. https://github.com/miraflor/net-distance-ph

**BibTeX:**

```bibtex
@misc{miraflor2026netdistanceph,
  author       = {Miraflor, James Matthew},
  title        = {{net-distance-ph}: Population-Weighted
                  Road--Ferry Network Distance Matrices
                  for the Philippines},
  year         = {2026},
  howpublished = {Computer software},
  institution  = {Scientific Computing Laboratory,
                  Department of Computer Science,
                  College of Engineering,
                  University of the Philippines Diliman},
  url          = {https://github.com/miraflor/net-distance-ph}
}
```

The MkDocs `repo_url` points to `miraflor/net-distance-ph`.
The source code is supplied under the MIT License (see [LICENSE](LICENSE));
OpenStreetMap and administrative geographic datasets retain their own licensing
and attribution requirements. Review the license before publishing.
[Publication checklist](docs/reproducibility.md).
