# Changelog

## Unreleased — repository and package name alignment

- GitHub repository, Python distribution, user-facing project name, and GitHub Pages path use `net-distance-ph`.
- The Python import package uses `net_distance_ph`; this is an import-path change, not an algorithm change.
- Environment settings use `NET_DISTANCE_PH_DATA`, `NET_DISTANCE_PH_WORK`, and `NET_DISTANCE_PH_OUT`.
- Existing automation importing the package or setting these environment variables must be updated.
- The `python run.py` CLI, command arguments, distance algorithms, source shapefile names, and output file formats are unchanged.

## Unreleased — verification integrity and acquisition provenance

- **Published-output verification:** `verify` now reconstructs the final
  locality matrix and row-validity radii from the core checkpoint, rather than
  checking only core-vs-unreduced graph distances. This rejects otherwise
  undetectable symmetric, triangle-inequality-preserving changes to the NPY
  output; it also rejects a changed `INCLUDE_SNAP_OFFSET` setting. Regression
  tests corrupt the published matrix and radii independently.
- **Cached OSM provenance:** a new PBF download records its actual acquisition
  URL and HTTP metadata in a sidecar. A legacy cache without such metadata is
  explicitly marked as having unknown acquisition provenance; it is never
  described as downloaded from the requested URL. Reuse with a conflicting
  documented URL fails instead of silently mislabeling source material.
- **Input validation:** nonpositive matrix batch sizes and checkpoint intervals,
  negative worker counts, and invalid CLI wave scales now fail with a clear
  message before the pipeline starts.
- **Topology limitations clarified:** geometrically coincident vertices do not
  prove that distinct OSM nodes form a traversable junction, especially at
  grade-separated crossings. The numerical model and all historical routing
  defaults remain unchanged.

## Unreleased — review of the source-independent pipeline

Distances computed with the default configuration are unchanged. Each fix has
a regression test that fails on the previous code.

- **Documented commands work again.** The byte-level input check made
  `reduce`, `matrix` and `verify` re-read the input shapefiles, so the
  documented `python run.py reduce` (without `--data`) looked in the
  repository's own `data/` folder and stopped. `graph` now records the input
  folder in `work/sources.npz`; later stages reuse it unless `--data` or
  `NET_DISTANCE_PH_DATA` is given. A wrong folder gives a message naming
  the recorded one.
- **`check_setup.py` reports every file again.** It used `run._find`, which
  raises `SystemExit`; the preflight stopped at the first missing file with a
  message telling the user to run `check_setup.py`. Input resolution now
  raises an ordinary exception (`run._locate`), reported as a FAIL line.
- **Ferry relation members.** Every member way of a ferry route relation was
  treated as a sea crossing: terminal platform polygons became closed ferry
  rings with gangway candidates, and approach roads were moved out of the road
  layer. Only route-role, non-highway, non-facility, non-area, open ways now
  count; highway-tagged members keep their road class.
- **Missing node on the PBF path** raised pyosmium's `InvalidLocationError`
  (not a `ValueError`), so the intended message was never shown. It is now
  caught and reported with the affected way IDs.
- New opt-in `--include-motorways` for `scripts/build_transport.py`
  (`motorway` + `motorway_link`); the default network is unchanged.
- The ferry shapefile's provenance listed the road-class filter; it now
  records the ferry rule.
- `pyproject.toml`: build requirement raised to `setuptools>=77`, which the
  SPDX `license = "MIT"` form needs (setuptools 70 failed to install).
- CI: the GIS job also installs the `transport` extra, so the new tests run
  the real pyosmium PBF path; previously only a fake module was tested.
- Removed unused imports; corrected a stale test skip reason; updated
  `pipeline.md`, `transport.md`, `validation.md` and the beginner guide.

## Unreleased — source-independent geographic pipeline

- Added a standalone two-pass OpenStreetMap transport extractor with road-class
  selection, ferry relation-member recognition, local/PBF input, explicit
  versioned source download, and auditable immutable transport releases.
- Converted boundary publication into transactional, versioned releases selected
  through a single atomic pointer. Failed centroid conversion cannot mix new
  GeoPackages with old point shapefile sidecars.
- Added content-hashed source manifests and byte-level cross-stage validation;
  a shapefile or sidecar change requires fresh graph and reduction stages.
- Published `out/source_manifest.json` to bind matrix outputs to source hashes,
  graph settings, and reduced-core fingerprints.
- Added offline transport relation tests, failed-publish tests, and deliberate
  same-size/same-mtime data-corruption regression tests.
- Removed unrelated project references from public documentation and code.
- Added a default MIT source-code license and anonymous contributor metadata;
  upstream geographic datasets retain their own licenses.
- Standardized worker startup on `spawn` across platforms to avoid fork-after-thread deadlocks; numerical computation is unchanged.
- The historical algorithm and default ferry snap policy are unchanged. The
  independent OSM extraction recipe is explicit and may yield different graphs
  from historical third-party shapefile exports.



## Pre-publication review corrections — 2026-11-05

Distances computed with the default configuration are unchanged (bit-identical
on the synthetic archipelago). Each fix below has a regression test that fails
on the previous code.

### Correctness

- **Stale matrix checkpoint.** Re-running in the same `work/` after the inputs
  changed resumed the old matrix checkpoint (only its shape was checked) and
  wrote the old distances to `out/`. The checkpoint now stores a fingerprint of
  the core graph and is discarded when it does not match.
- **Mixed stage files.** Running `graph` and then `matrix` without `reduce`
  combined new locality IDs with the old core and exited successfully,
  attaching distances to the wrong localities. Stage 2 now records which
  stage-1 files it was built from; `matrix` and `verify` refuse a mismatch.
  Stage-1 settings in `config.py` are also recorded and checked.
- **Verification could not fail where it claimed to.** It checked symmetry
  after the matrix had been made symmetric, used `assert` (skipped under
  `python -O`), and reported triangle-inequality violations as expected. It now
  checks the pre-symmetrisation residual, treats triangle violations as
  failures, writes `PASS`/`FAIL` lines, exits non-zero on failure, and refuses
  an `out/` folder that does not match the core in `work/`.
- **Triangle inequality documentation was wrong.** Adding connector offsets at
  both ends does not break the triangle inequality; it only adds slack.
  Corrected in `run.py`, methodology, validation and the historical notes.
- **Numeric ID fields.** A numeric PSGC field with a blank produced IDs such as
  `1380600000.0` and `nan` that passed validation. Such columns are rejected.
  IDs and names are no longer truncated to fixed widths (32/96 characters).
- **Null-shape records.** A polyline shapefile whose last record is a Null
  shape crashed the reader.
- **Name column.** Auto-detection could pick the ID column as the name column
  (`MUN_CODE` matches the name pattern); it now skips the ID column.
- **Boundary builder provenance.** A refused rebuild (no `--replace`)
  overwrote `boundary_provenance.json` of the existing GeoPackage, and an
  incomplete `--replace` attempt overwrote the published audit. Failed attempts
  now write to `boundary_build_incomplete/`; published files change only on
  success. Cached geoBoundaries geometry is now paired with the API metadata
  saved at download time, instead of metadata fetched later for a possibly
  newer release. Redirect targets of downloads are checked against the host
  allowlist.
- **Missing codes in the converter** became the text `"None"` with pandas 2.x
  and passed validation.

### Operation

- Ctrl-C during `matrix` with several workers hung (the pool waited for every
  queued task). Workers now ignore SIGINT; the parent terminates the pool and
  saves finished rows. macOS uses the `spawn` start method instead of `fork`.
- Files in `out/` are written atomically (temporary file and rename).
- CSV outputs use the `csv` module: names keep double quotes exactly (they were
  replaced by single quotes) and fields with commas are quoted correctly.
- New option `SNAP_FALLBACK_TERMINALS_ONLY` (default `False`, historical) to
  stop roadless towns from attaching to mid-route ferry shape points.
- `GEODESIC_BACKEND` now also applies to gangway and connector lengths.
- `check_reduce_status.py --work DIR`; friendlier `--waves` errors; a clear
  error when no locality is within the snap radius.

### Repository

- CI: the main job failed on every platform (the boundary-builder tests needed
  pandas/geopandas, which `[test]` does not install). Those tests now skip
  there; the GIS job runs the whole suite. A docs job builds with `--strict`
  on pull requests.
- GitHub Pages: `mkdocs build --strict` aborted on a link outside `docs/`.
  Mermaid diagrams rendered as plain code; the custom fence is now configured.
- A bare `pytest` (not only `python -m pytest`) now finds `run.py`/`config.py`.
- Corrected the Lambert accuracy claims in `geo.py` (1.4e-6, not 1e-8); a
  wrong example filename in the boundary docs; removed unused imports.

## Boundary-source independence revision — 2026-11-05

- Replaced the proposed shared prebuilt-boundary reuse with `scripts/build_boundaries.py`,
  a standalone PSA PSGC + geoBoundaries ADM3/ADM2 source-and-reconciliation workflow.
- Added metadata/source hashes, auditable deterministic matching, exact-coverage
  enforcement, explicit reviewed overrides, and optional source-only exclusions.
- Clarified the observed source-vintage conflict (1,647 ADM3 source features versus
  1,642 PSGC Q2 2026 localities) and dataset-specific attribution terms.
- Added offline synthetic tests for the independent workflow. The **live**
  national data build remains unverified; do not describe this as an audited
  1,642-locality publication without rerunning against upstream snapshots.

## 0.1.0 — GitHub-ready source distribution (2026-11-05)

### Preserved from the historical implementation

- Original WGS84 road/ferry edge construction, endpoint-only ferry connectors,
  centroid snapping, exact graph reduction, RCM reordering and Dijkstra waves.
- Resumable reduction/matrix checkpoints, symmetry checks and direct
  verification against the unreduced graph.
- Original configurable route-cost and snapping assumptions.

### Improvements

- Standardized repository and import package naming (subsequently renamed to `net-distance-ph`).
- Removed machine-specific paths in configuration, tests and VS Code guide;
  introduced portable environment-variable paths and `--data`, `--work`,
  `--out` CLI overrides.
- Fail-fast detection of duplicate input filenames, missing/duplicate source IDs
  and invalid centroid geographic coordinates.
- New optional `scripts/centroids_from_boundaries.py` to prepare WGS84 point
  inputs from an independently sourced, PSGC-keyed polygon dataset.
- Converted historical synthetic-archipelago test into a real CI regression;
  added boundary-converter and path-resolver tests.
- Added packaging metadata, clean ignore rules, Linux/Windows CI,
  documentation for source lineage and modeling assumptions, and MkDocs-based
  GitHub Pages publishing workflow.

### Explicitly not changed

- No historic distance values were recalculated.
- No external GIS data, third-party project code, road graphs or ferry graphs were copied.
- No code license was selected; choose one before public distribution.
