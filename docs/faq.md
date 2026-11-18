# Frequently asked questions

### Does this create real driving-time estimates?

No. It measures geometric path length in kilometres on a road/ferry graph. It
has no speed limits, traffic data, route schedules, one-way/turn restrictions,
or transfer delays. Optional ferry multipliers create generalized equivalent-
kilometre **costs**, not time.

### Is the matrix symmetric?

The model uses undirected edges, so yes (after the final numerical symmetry
check and postprocessing). Actual travel can be directional; that is not
modeled here.

### Is it a metric (does the triangle inequality hold)?

Yes, with or without the connector offsets; `verify` checks it on a sample
of triples. See [methodology](methodology.md).

### Why are some pairs infinite even in the completed matrix?

Some localities are attached to disconnected network components or not snapped
at all. A ferry may be missing from the chosen snapshot. It means
**unreachable in this graph**, not necessarily in real life.

### What if a partial matrix contains infinity?

Use `row_validity_radius_km.npy`. A finite radius means the search for that
row is incomplete; an infinite radius means its unrestricted Dijkstra run
finished. Check [output interpretation](outputs.md).

### Why not use straight-line centroid distances instead?

Euclidean or geodesic distances ignore roads and ferry links. The network
matrix captures some detours and discontinuities, but introduces its own
model and data-quality assumptions.

### Are towns represented by municipal halls?

Not by default. The point input represents geographic reference points, and
the optional boundary converter uses geometric centroid or representative
point. These are not municipal hall coordinates or population centroids.

### Can I generate municipal boundaries without any other project?

Yes. Use `python scripts/build_boundaries.py --output data`. It sources the
PSA PSGC roster and geoBoundaries polygons independently, writes matching
audits, and generates a locality GeoPackage and point shapefile **only when
coverage is verified**. The two public sources have different vintages, so
reviewed overrides may be needed. Road/ferry lines are separately constructed using the
[independent transport builder](transport.md), which can download an OSM
Philippines PBF and extract both way-tagged and relation-member ferry ways.
The boundary builder and transport builder each select a versioned output
release; the matrix runner resolves both selections automatically.
Read the [boundary guide](boundaries.md).

### Can I build the national transport network myself?

Yes. Run `python scripts/build_transport.py --download --output data`
after installing the `[transport]` extra. A cached OpenStreetMap extract is
used to generate WGS84 road and ferry line shapefiles. The result has a
content-hashed provenance manifest and is not a statement about ferry
operating status, road restrictions, or full national network correctness.
See [transport inputs](transport.md).

### Can I reuse a matrix after changing boundary inputs?

No, not if origin coordinates or identifiers change. Build a new matrix and retain its source provenance.

### Why is `work/` so large and ignored by Git?

It contains arrays for the national graph, reduced CSR, resume checkpoints
and temporary matrices. Those can be regenerated from original inputs; they
are excluded from the source repository to prevent accidental distribution of
large or licensed datasets.

### How can I test whether every town is reachable?

After building the graph, verify full official PSGC coverage and snapping, then test whether all town-attached graph nodes share a single connected component. Real-world ferry and road availability still require independent auditing.

<!-- v55-methods-documentation -->
## Population-weighted workflow questions

### Which distance matrix does `net-distance-ph population-weighted` generate by default?

**Town, province-equivalent and region**, all using **balanced connected-component-aware network k-medians** and one common routing calculation. Use `--level town`, `--level province`, or `--level region` to request just one. The legacy centroid pipeline has different commands and semantics. See [the population-weighted method](population_weighted.md).

### Are province-equivalent distances weighted by barangay population?

**Yes.** The initial medoid site weights are sums of official barangay populations. Town OD pairs contribute reachable population-pair **numerators and denominators**, which are summed across province-equivalent/region membership before division. This is **not** a simple mean of town distances, and it is not a new province-centroid route calculation.

### Why does a town or province-equivalent have a nonzero diagonal?

It is a weighted expectation over independently chosen representative sites in the same administrative area, **conditional on a modeled route**, rather than the distance from one fixed centroid to itself. A conventional zero diagonal should not be imposed without changing the estimand.

### What if some barangays have no geometry or cannot be snapped?

Their **full census population stays in the denominator**; they have no fabricated road path. `origin_population_coverage.npy` distinguishes that missing origin representation from actual graph disconnection. `reachable_pair_probability.npy` measures the share of census-pair mass whose representatives have a proven modeled route, not observed trip likelihood.

### Is balanced k selection a standard off-the-shelf optimal-k formula?

No. The **network p-median problem** is canonical, but the implemented normalized access-loss plus extra-site-fraction balancing rule is **project-specific**. The k=1 candidate-demand-node median is exact; greedy and 1-swap solutions for larger k are heuristic, and a per-town cap applies across all disconnected components. See [methodology](population_weighted.md#4-connected-component-aware-network-k-medians) and [literature](literature.md).

### Are all 43 nonprovincial towns necessarily HUCs/independent component cities?

No. The existing normalized input distinguishes missing province parents and city versus municipality, **not** a verified legal subclass. Province-equivalent groups include province-less municipalities (for example Pateros and SGA municipalities) as separate peers. The software intentionally does not infer HUC or ICC labels from missing parents.

### How do I explore the distance matrices and compare output levels?

Run `net-distance-ph view` in the installed environment after completing a
population-weighted run. This launches a **local** browser explorer with
selectors for completed runs and available town, province-equivalent, and
region matrices. Click or search for a location, adjust **Closest X**, and
inspect the ranked finite distances and reachability counts. The current
boundary release and simplified road/ferry overlays provide optional map
context. The viewer only reads scientific outputs; its display cache does not
replace them. See [the CLI instructions](cli.md#local-output-explorer-view).

### Can GitHub Pages display my local national matrices automatically?

No. GitHub Pages hosts the **documentation**, not your local matrices. The
explorer runs at a loopback address such as `http://127.0.0.1:<port>/` and
reads files from your computational workspace; it does not upload them.
Keep the viewer terminal open and press **Ctrl+C** when finished.

### Where are the default population-weighted matrices?

The same `net-distance-ph population-weighted` run exports **town, province-equivalent and region** matrices. The last console block reports all three exact directories, the distance CSV/NumPy, reachability matrix, unit IDs and the index `latest_run.json`. To select one tier, add `--level town|province|region`. To reuse the exact polygons from the prior successful run, omit `--geometry-source` (the new `latest` policy); explicit providers are still supported.
