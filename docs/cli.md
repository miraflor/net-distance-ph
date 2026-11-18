# Unified command-line interface

<!-- v55-methods-documentation -->

**Population-weighted default:** `net-distance-ph population-weighted` exports **town, province-equivalent, and region together** with **balanced component-aware k-medians**. Specific-level selections `--level town`, `--level province` (province equivalents), and `--level region` are optional and use the same town routing computations. See [population-weighted definitions](population_weighted.md), [outputs](outputs.md) and [literature](literature.md). The `net-distance-ph run` command is the separate baseline centroid pipeline.

**Default behavior:** `net-distance-ph run --until connectivity --verbose` now
validates and publishes the checksum-pinned PSGC-aligned town source automatically.
Use `--boundary-level {town,city,municipality,huc,province,region,barangay}`
(default `town`) to select geographic origins. Non-town levels use separate
`workspace/levels/<level>` folders so their graph and matrix artifacts cannot mix.
See [boundary levels](boundary_levels.md) for source and approximation cautions.

**Alternative 2026 PSGC-aligned geometry:** `net-distance-ph boundaries --download` validates the pinned third-party ADM3 ZIP; append `--publish` only after reviewing provenance. See [the boundary-source instructions](psgc_aligned_boundaries.md).

`net-distance-ph` is the installed command for source preparation, nationwide graph
construction, localities' modeled connectivity, graph reduction, matrix
calculation, and numerical verification. It works from any working directory
once the project is installed into your Python environment.

Install (in the desired Conda environment):

```powershell
python -m pip install -e ".[test,geo,transport]"
net-distance-ph --help
```

## Road selection default

The default `--road-profile comprehensive` now extracts fourteen motor-road
classes (including motorways, motorway ramps, residential and service roads).
`--road-profile major` selects the historical four classes (though newly applied access filtering may change individual included ways). For a
versioned previous transport release, the next `prepare` stage verifies the
cached PBF SHA-256, archives the old stage-1 graph/audit if present, and creates
a new immutable transport release. It does **not** download a replacement PBF.
See [road profiles](road_profiles.md) for the exact list and limitations.

## First national connectivity experiment

```powershell
net-distance-ph status
net-distance-ph doctor
net-distance-ph run --until connectivity --verbose
```

`doctor` is read-only and reports missing inputs without downloading anything.
`run --until connectivity` prepares sources if necessary, constructs the
road/ferry graph, audits official PSGC city/municipality coverage and graph
connected components, then stops **before** reducing the graph or computing the
all-pairs matrix.

If the boundary builder cannot reconcile the selected PSA roster with the
selected boundary source, the command stops. It does not invent missing
localities or links. If the modeled network is disconnected, `connectivity`
returns nonzero and a range containing that stage stops there. A successful
component check does **not** validate current ferry services, roads, turn
restrictions, or legally traversable paths.

## Commands

| Command | Behavior |
| --- | --- |
| `net-distance-ph status` | Show input and output file presence, without performing or validating a run |
| `net-distance-ph doctor` | Inspect GIS Python packages and input shapefile headers, without fetching |
| `net-distance-ph prepare` | Download/reconcile input releases when necessary |
| `net-distance-ph graph` | Build stage-1 road/ferry graph and town attachments |
| `net-distance-ph connectivity` | Audit roster, snapping and connectivity; no matrix needed |
| `net-distance-ph reduce` | Reduce the graph without changing modeled distances |
| `net-distance-ph matrix` | Compute/resume distance rows |
| `net-distance-ph verify` | Check computed distances against the unreduced graph |
| `net-distance-ph run --until connectivity` | Prepare, graph, connectivity |
| `net-distance-ph run --from reduce` | Reduce, matrix, verify; use existing graph |
| `net-distance-ph run --only matrix` | Execute just the matrix stage |
| `net-distance-ph run --all` | Prepare, graph, connectivity, reduce, matrix, verify |

Pipeline execution stops on the first nonzero exit status. In particular,
`run --all` **does not bypass** a connectivity failure. If you decide to
compute distances for reachable subgraphs of a disconnected graph, make that an
explicit methodological decision and run stages individually rather than
silently ignoring the audit.

All options follow their subcommand, for example:

```powershell
net-distance-ph graph --workspace "D:\Research\net-distance-ph" --verbose
net-distance-ph run --from reduce --workspace "D:\Research\net-distance-ph" --workers 4
net-distance-ph matrix --workers 4 --waves "1,2"
net-distance-ph reduce --restart-reduce
net-distance-ph status --json
net-distance-ph connectivity --help
```

By default the workspace is `~/Documents/net-distance-ph` with `data/`, `work/`,
`output/`, and `logs/`. The CLI passes all stage execution to the existing,
provenance-aware runner. `python run.py ...` is retained as a backward-compatible
interface, as is `net-distance-ph-connectivity`.

`status` reports **file presence**, not verified results. `doctor` checks
package import availability and input file headers but is not a geographic
accuracy audit. For rigorous national publication, retain input provenance,
review long centroid snaps and ferry terminals, and run final verification.

## Inspect disconnected graph topology (read only)

`net-distance-ph diagnose-connectivity --top 43` reads the existing stage-1 graph and reports candidate intercomponent road-vertex gaps without editing graph or sources. See [Connectivity diagnostics](connectivity_diagnostics.md).

## Examine the cached OSM source at component gaps (read only)

`net-distance-ph inspect-osm-gaps --top 9 --radius-m 1000` scans the cached OSM PBF and tests whether omitted road classes create local shared-node paths. No roads or graph edges are changed. See [OSM gap inspection](osm_gap_inspection.md).

## Factory reset and clean-room experiment

`net-distance-ph reset` is preview-only unless you add `--execute` and type an
explicit confirmation phrase. This command does not change Git history and
never deletes source committed to the current branch.

```powershell
net-distance-ph reset                                  # preview workspace deletion
net-distance-ph reset --all --purge-patch-artifacts     # inventory every cleanup target
net-distance-ph reset --all --purge-patch-artifacts --execute
net-distance-ph reset --all --purge-patch-artifacts --execute --run
```

`--all` requires a clean, pushed checkout tracking `origin/<current branch>`;
execution fetches that branch and refuses any local/remote commit mismatch.
It performs `git reset --hard` to that same commit and `git clean -ffdx`, then
removes the entire selected `~/Documents/net-distance-ph` workspace. Optional
`--purge-patch-artifacts` removes **only** the enumerated historical patch ZIPs,
extracted installers, review bundle, and backup directories. Never use this
command if you need data from those paths; this deletion is irreversible.

Add `--run` to the destructive `--all` reset to rebuild from scratch with per-
stage timing: `prepare`, `graph`, then `population-weighted` using balanced
adaptive k-medians and Faeldon barangay geometry. This is independent of the
centroid-based reduce/matrix pipeline. Fresh-run journal/summary and timestamped
stage logs are stored under `~/Documents/net-distance-ph/logs/`.

## Compact elapsed-time logs (v47.1)

All logged console and archive lines now use the same short local timestamp
(no date or timezone) and a compact `e=MM:SS` cumulative elapsed clock. When launched
by `net-distance-ph reset --all --execute --run`, the run clock begins before
`prepare` and continues through all subprocess stages. For an individual CLI
command, it begins when that command starts. Stage messages carry
a compact stage duration, with precise seconds in the JSON journal. Nested phases print
only the deepest active heartbeat.

Each session writes both a `run-*.log` and a `run-*.jsonl` in workspace
`logs/`. Fresh-reset runs append phase and routing progress events directly to
`fresh_run_journal.jsonl`; `fresh_run_summary.json` includes the measured
`phases` as well as the three outer stages. Routing ETA is an estimate from
observed processing speed, not a guaranteed finish time. Historical logs
remain unchanged. Installing logging never invalidates or recomputes matrices.

## Resource telemetry and evidence report (v48)

Install optional process-tree CPU/RAM sampling with
`python -m pip install -e ".[monitor]"` (uses `psutil`). Without the extra,
timing works but resource statistics are marked *unavailable*. The sampler
reports observed peak **summed process-tree RSS** and **CPU core-equivalents**;
shared pages may be counted more than once and brief spikes may be missed.
Sampling does not verify routing correctness.

`net-distance-ph observe` analyzes existing workspace logs and writes
`logs/observability_report.json`, without executing the pipeline or deleting
outputs. `net-distance-ph observe --json` prints the complete report; add
`--no-write` to avoid creating a report file. For future fresh-run commands,
`logs/fresh_run_observability.json` is written automatically.

Known-denominator progress includes counts, throughput, and an observed-rate
ETA. Road extraction reports throughput but intentionally says *ETA unknown*
because the total accepted ways cannot be known ahead of parsing. Other
indeterminate phases retain accurate elapsed/resource heartbeats without
manufactured completion percentages.

The evidence report distinguishes actual sampled numerical verification and
population coverage from *not assessed* national connectivity, legal boundary
validity, and real-world ferry service validity. A successful exit status
is not a scientific certification. Historic results and logs are not changed.

## Geographic and population coverage audit (v49)

On an **existing completed population-weighted run**, inspect administrative
exclusions, observed population-to-population reachability, component membership,
and road/ferry graph topology *without rerunning routing or source preparation*:

```powershell
net-distance-ph audit-geography
net-distance-ph audit-geography --skip-graph  # faster, population and province only
net-distance-ph audit-geography --top 200
net-distance-ph audit-geography --all-pairs   # potentially large streaming CSV
```

The command verifies the saved population input table hashes and matches town,
province, representative-site, and reachability outputs by routing fingerprint.
If the original run used `--province-crosswalk`, pass the original crosswalk
again using `--province-crosswalk PATH`; its SHA-256 must match the run manifest.
No administrative parentage or new route is inferred.

**Outputs:** `workspace/output/geographic_audit/<routing_hash_prefix>/` contains
`geographic_audit.json`, `province_excluded_towns.csv`,
`population_components.csv`, `town_component_memberships.csv`,
`top_zero_reachability_town_pairs.csv`, and
`top_partial_reachability_town_pairs.csv`.
`--all-pairs` also streams `all_noncomplete_town_pairs.csv`.

By default, it additionally invokes the repository's **existing** stage-1
connectivity auditor with its source-provenance checks, writes road-only and
road+ferry official-town connectivity reports beneath `stage1_connectivity/`,
and identifies original-graph offshore ferry endpoint nodes lacking a direct
road/gangway attachment (`unattached_ferry_terminals.csv`). The original graph
has more than 12 million nodes and this part is memory-intensive. `--skip-graph`
leaves these two sections *unassessed*, not passed.

A town with sites in different graph components is **not** forced into one
component. Pairwise reachability reports separately distinguish zero modeled
routes, partial modeled reachability, complete modeled reachability, and
unmodeled origin population. **No direct ferry gangway** does not by itself
prove network disconnection; nearest-town names are geographic context, not
shortest routes. This audit does not certify real-world ferries, legal
administrative assignments, or the practical usability of modeled routes.

Existing graph, matrix, source, and routing checkpoint files are never changed;
only new audit reports and a session log are written.

## Representative-site topology diagnostic (v50)

After `net-distance-ph audit-geography`, audit the **saved national graph versus
barangay-preserving core** without rebuilding, changing any edges, computing
shortest paths, or regenerating k-medians:

```powershell
net-distance-ph audit-topology --lineage-only
net-distance-ph audit-topology
# Optional targeted scope (diagnostic only; not model parameters):
net-distance-ph audit-topology --top-components 20 --search-km 20
```

1. Verifies current graph bytes against the cache's original graph SHA-256,
   cross-checks core/route/population hashes, and re-computes connected components
   on both the original stage-1 and reduced barangay-preserving graphs.
2. Checks **every saved selected representative**: the reduced component must
   match the saved label; each populated original component must map to exactly
   one reduced component and vice versa. A mismatch is a model-integrity failure;
   geometric gaps are **not** inferred after such a failure.
3. Reports, for every town/component, its modeled population, other towns in
   that exact same component, and whether its centroid is in the same component.
   Thus a submunicipal population area that shares another municipality's
   network component can be distinguished from a genuinely isolated component.
4. For the largest nonmain populated components, searches original graph
   vertices within the stated spatial radius for nearby vertices in another
   component and separately in the national majority component. Each candidate
   includes geocoordinates, nearest geometric gap, graph-node types and degrees.
   These are **triage leads**, not proofs that a real connection exists or should
   be inserted. No candidate within 10 km does not prove physical isolation.

Output: `workspace/output/topology_diagnostics/<routing_hash_prefix>/`:
`topology_diagnostics.json`, `selected_site_components.csv` (all selected
representative nodes and coordinates), `town_component_access.csv` (including
cross-town component membership), `priority_components.csv`, and
`candidate_geometric_gaps.csv`.

**Memory warning:** Reads the saved 12-million-node original graph and computes
its connected components; requires RAM, and the file SHA-256 scan adds I/O.
If a locality has a disconnected representative, the k-median selection
cannot create edges; this audit first determines whether the disconnection is
already present in the original network or introduced by graph reduction.

It does not adjudicate ferry operations, one-way legality, real-world bridge
availability, or whether a close pair should be connected. Only new audit
reports and logs are written; matrices, cached graph, source files, and
representatives remain unchanged.

## Population-gap original OSM node-ID investigation (v51)

After `net-distance-ph audit-topology`, use the original **cached** OSM PBF to
inspect component gaps found around populated representatives (no graph edits):

```powershell
# Five urban cases, in v50's original ranking:
net-distance-ph inspect-topology-osm --ranks 2 6 7 8 9 --radius-m 1000
# All valid candidates among the 15 largest non-main population components:
net-distance-ph inspect-topology-osm --top 15
```

Reuses the repository's existing `osm_gap_inspection` implementation of local
path search over actual **shared OSM node IDs**. Reports original OSM way IDs,
nearby road classes and access tags, and witness node-ID paths under baseline,
expanded ordinary-road and service-road scenarios. Validates routing hash,
original graph SHA-256 and stage-1 source fingerprints, transport release
provenance, and cached PBF SHA-256 before running. The selected OSM file is read
once with a locations-enabled PBF scan; the full-country scan and `flex_mem`
location index may be resource-intensive. `osmium` is needed; use
`python -m pip install -e ".[transport]"` if missing.

Outputs live in `workspace/output/topology_osm_investigation/<routing_hash_prefix>/`:
`investigation_summary.json`, `gap_findings.csv`, `nearby_osm_ways.csv`,
`endpoint_osm_node_ids.csv`, and `local_osm_paths.geojson`.

**Interpretation constraints:** nearest geometric gap is not a missing road;
local shared OSM ID paths are undirected topology only, not validation of road
access, bridges, grade separation, or a functioning ferry. Absence of a local
path is inconclusive outside the search window. Ferry routes are reported as
nearby ways but are not included in the inspector's road-only BFS scenarios.

No network edges, source data, model outputs, or routing parameters change.

## Population-origin snap sensitivity audit (v52)

Review population origins in isolated components **without changing their snaps**:

```powershell
# Same components as urban OSM investigation; ranks match v50 priority components.
net-distance-ph audit-origin-snap --ranks 2 6 7 8 9
# Or top 15 non-main components:
net-distance-ph audit-origin-snap
```

Reads the existing population core's original-node indices, saved snap offsets,
positive-population barangay origins, representative sites, and exact graph
connectivity. Loads the **original geometry source** only when all its SHA-256
files match the core metadata; tries the existing pinned cache and population
reference points, never downloads. An explicitly supplied `--points-file PATH`
must also match the recorded hash. No geometry can be inferred from snapped
vertices alone. The default full original graph creates a potentially large
KD-tree of main-component road nodes and may require several GiB of RAM.

For each selected non-main component, the command finds the nearest *road node*
in the largest populated component by ECEF chord, reports its WGS84 geodesic
origin offset, and compares it with the original run's saved snap offset.
Marks whether that alternative lies inside the **same run's** `max_snap_km`
limit. This is a distance-based *sensitivity test*, not permission to move
population, add a missing road, override access restrictions, or reroute a town.
It does NOT prove legal vehicle or pedestrian access, nor establish physical
connection across water, bridges, or private properties. Near-ties in ECEF
ranking may differ from exact WGS84 nearest-road rankings.

CSV and JSON outputs: `workspace/output/origin_snap_sensitivity/<routing_prefix>/`
- `affected_barangay_origins.csv`: the saved choice and alternative per barangay
- `component_sensitivity.csv`: affected population and within-radius fractions
- `origin_snap_sensitivity.json`: SHA-256 provenance and scientific cautions

Results from the earlier v51 OSM audit about restricted roads are independent
context; **no restricted-road paths are treated as routable** in v52.

## Six-barangay polygon/network validation (v53)

`net-distance-ph audit-polygon-network` checks the **same six v52 urban
barangays** (Paliparan III; Santo Niño; Punta Princesa; Caloocan 181 and
182; New Cabalan) against their **pinned and SHA-256-verified ADM4 polygons**.
This is a bounded read-only GIS quality-assurance operation, not another
national search or a revision to road or population geometry.

Requires the cached pinned `PH_Adm4_BgySubMuns_complete.shp.zip`, exact
`barangay_points_faeldon_*.csv` from the original population run, and the
existing v52 origin-snap sensitivity report. It refuses changed source
bytes and does not download anything. For six polygon shapes only, it tests:

- origin/current snap/alternative main-road point-in-polygon (`covers`);
- intersections between polygon geometry and **existing eligible ROAD graph
  segments**, grouped by original graph connected component;
- total intersecting road-segment lengths and point-to-road-segment distances
  in a local azimuthal-equidistant metric projection;
- v51 gap-window OSM access-tag counts as **context**, not evidence that an
  individual road belongs to the barangay or provides valid access.

Writes `workspace/output/polygon_network_consistency/<run-prefix>/`:
`polygon_network_consistency.json`, `six_barangay_findings.csv`,
`six_barangay_overlay.geojson` (source polygon, original point, two snapped
road vertices, nearest in-polygon road segments for each component).

**Do not use this report to invent junctions, connect roads across water,
override access restrictions or forcibly move a barangay's population.**
A road inside a polygon may still be inaccessible from a particular site;
its presence is not proof of passable street access or accurate population
distribution. Individual polygons with invalid source geometry are repaired
for *measurement only* and explicitly flagged. For other geometry policies
(e.g. OCHA, custom points), this implementation refuses rather than silently
substitute a different polygon release.

## v54–v56: province-equivalent layer and consolidated default

`net-distance-ph population-weighted` now defaults to **`--level all`** and
**`--median-selection balanced`**. Balanced selection is the existing
component-aware network k-medians; it does not force connections between
road/ferry components. The default exports town, province-equivalent and region
distance and population-pair reachability matrices in one run. All work is derived from the same
saved graph, barangay points, and official barangay counts.

Explicit output selection:

```powershell
net-distance-ph population-weighted                          # ALL THREE matrices
net-distance-ph population-weighted --level province           # province equivalents only
net-distance-ph population-weighted --level province-equivalent # synonym
net-distance-ph population-weighted --level region             # region only
net-distance-ph population-weighted --level town               # town only
```

`--level province` is a compatibility alias, but **it no longer exports a
province-only matrix**. The output is
`variants/population_weighted/output/<routing-hash>/province_equivalent/`.
The historical `province/` output, when present, is left untouched.
`province_equivalent/units.csv` includes a `unit_type` column:
`province`, `nonprovincial_city`, or `nonprovincial_municipality`. The normalized
PSGC tables do not encode the HUC/ICC legal distinction; we make no speculative
claim about which independent cities are HUC or ICC. Province-less
municipalities, including Pateros and SGA localities, are included as their own
units rather than lost. Each town is mapped exactly once. An explicit
`--province-crosswalk` can override an empty parent only when the user supplies
a validated assignment; no inferred assignment is made by default.

Higher-tier matrices aggregate the same completed town-level routing
numerators and reachable-population denominators, so their population and
reachability accounting is conservative. They do not cause separate graph
construction or separate intersite shortest-path searches. The output pointer
`latest_run.json` lists only the levels requested in that invocation.
This CLI change does not run or modify any existing computation by itself.

### v56: exact output locations and geometry reuse

On successful completion, the CLI prints the full **OUTPUT [TOWN]**, **OUTPUT [PROVINCE_EQUIVALENT]**, **OUTPUT [REGION]** directories, each distance CSV and NPY, reachability NPY, `units.csv` row index, and **LATEST OUTPUT INDEX** (`latest_run.json`). The index also contains a `matrix_outputs` object with these file paths. Omitted `--geometry-source` defaults to `latest`: reuse the exact SHA-256-verified cached source of the last completed run; if there is no prior run, use OCHA as before. Explicit `--geometry-source faeldon|ocha|auto` and `--points PATH` take precedence. An invalid prior-run source is an error, not grounds for silently downloading a replacement.
