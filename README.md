# phnetdist

**Reproducible road-and-ferry shortest-path distances between Philippine cities and municipalities.**

The project builds an undirected graph from independently prepared or
user-supplied OpenStreetMap road and ferry-route shapefiles, attaches locality reference points, contracts the
network without changing shortest-path distances, and computes a square matrix
in kilometres. It is designed for national-scale runs with resumable stages,
clear row/column identifiers, and empirical verification against the original
unreduced graph.

**[Documentation (GitHub Pages after deployment)](https://miraflor.github.io/phnetdist/)** ·
[Methodology](docs/methodology.md) · [Inputs](docs/inputs.md) ·
[Independent boundary generation](docs/boundaries.md) ·
[Independent transport extraction](docs/transport.md) · [Validation](docs/validation.md)

## Quick start

Python 3.11 or newer; run from the repository root:

```bash
python -m pip install -e ".[test]"
python -m pytest -q
python check_setup.py --data "/path/to/shapefiles"
python run.py all --data "/path/to/shapefiles" --workers 4
```

The input directory must contain these WGS84 shapefiles (subfolders are allowed):

| Input | Required basename | Purpose |
| --- | --- | --- |
| Road polylines | `primsectertrunk_road.shp` | Road network edges |
| Ferry polylines | `ferry_route_shape.shp` | Sea connections |
| Locality points | `muncity_centroids.shp` | Matrix origin/destination points, text IDs and labels |

Configure input, work and output locations using `--data`, `--work`, `--out`,
or the `PHNETDIST_DATA`, `PHNETDIST_WORK`, and
`PHNETDIST_OUT` environment variables. Default paths are `data/`,
`work/`, and `out/` under the project root. The three source filenames,
parameters and stages remain configurable in `config.py`.

```bash
python run.py graph --data "/path/to/shapefiles"
python run.py reduce --data "/path/to/shapefiles"                 # resumable contraction checkpoint
python run.py matrix --data "/path/to/shapefiles" --workers 4    # resumable matrix rows/waves
python run.py verify --data "/path/to/shapefiles"                # compares with unreduced graph
```

## Main outputs

`out/distance_matrix_km.npy` and `.csv` contain the distances (kilometres);
`out/towns.csv` fixes the row/column order. `out/row_validity_radius_km.npy`
distinguishes incomplete rows from complete rows containing unreachable
pairs. `out/matrix_report.json` and `out/verification.txt` summarize results.

An `inf` entry in the NumPy matrix is **not always an unreachable route**:
during a partial run, it may be a pair beyond that row's computed radius.
CSV blanks likewise do not distinguish these two cases. See the
[output contract](docs/outputs.md).

## Acquire source data independently

The project includes two optional standalone preparation tools. They acquire upstream
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

- **Preserved:** original network, contraction, neighbor-wave, and verification algorithms. With the default configuration the computed distances are bit-identical to the historical implementation on the synthetic test archipelago.
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

The `repo_url` is intentionally unset until you know the destination repository.
The source code is supplied under the MIT License (see [LICENSE](LICENSE));
OpenStreetMap and administrative geographic datasets retain their own licensing
and attribution requirements. Review the license before publishing.
[Publication checklist](docs/reproducibility.md).
