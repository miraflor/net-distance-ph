<!-- v55-methods-documentation -->
# Population-weighted road-and-ferry OD method

This page describes the **implemented default model** on the `feat/population-adaptive-k-medians-timing` code line after v56. It is distinct from the [legacy one-centroid-per-locality pipeline](methodology.md). The primary analytical unit is a **town (city or municipality)**. By default, town, province-equivalent and region matrices are produced together as *aggregations of the same routing results*.

## 1. Commands and defaults

| Option | Implemented behavior |
| --- | --- |
| `net-distance-ph population-weighted` | **All three levels**; component-aware, **balanced k-medians** |
| `--level town` | Explicit single-level request; town only |
| `--level province` / `--level province-equivalent` | Only a **province-equivalent** matrix; historical `province/` is not overwritten |
| `--level region` | Only region matrix |
| `--level all` | **Default**; town, province-equivalent and region matrices |
| `--median-selection balanced` | Default automatic k-selection *within each populated graph component*, subject to a **town-wide** 12-site cap |
| `--median-selection one` | One network median **per populated component**, not one per town |
| `--max-mean-access-km X` | Explicit budgeted greedy/1-swap selection instead of the balanced policy |
| `--max-sites-per-town 12` | Default global cap per town, **not** per component |
| `--max-snap-km 25` | Default maximum origin-to-graph snapping radius |
| `--geometry-source latest` | **Default**: reuse exact hash-verified cached geometry from last completed run; if none exists, use OCHA |
| `--geometry-source ocha` | Explicit OCHA-only source; unmatched barangays audited as unmodeled |
| `--geometry-source faeldon` | Explicit use of the separately pinned PSA-aligned polygon derivative |

The default preserves source identity from the **latest completed run** when its exact point CSV remains in the local cache and all recorded SHA-256 hashes agree; if no completed run exists, the original OCHA-first-run policy applies. For explicit reproducibility of your current Faeldon national run, use:

```powershell
net-distance-ph population-weighted --geometry-source faeldon --no-auto-geometry
# This one command now writes town, province-equivalent and region outputs.
```

The CLI prints the exact file and folder paths for every requested level and records them in `latest_run.json`. Use `--level town|province|region` only when a subset is desired. An existing latest run with lost or changed geometry is rejected rather than silently switched to a different source.

The population-weighted workflow requires the saved stage-1 road/ferry graph and compatible sources. It reuses the graph, population core, medoid-site checkpoints and source-to-site routing checkpoints when their fingerprints agree. Selecting an upper tier does **not** build a new graph or define new upper-tier medoids; OD routing may still need to run if matching town checkpoints are absent.

## 2. Administrative and population inputs

The flat input tables are `units.csv` (explicit ten-character PSGC codes, levels and parents) and `barangay_population.csv` (nonnegative integer populations). The bundled normalized data are derived from the **PSA Q2 2026 PSGC** roster and **2024 barangay populations**. The relevant source and input SHA-256 values are recorded. Any alternative tables must preserve their own consistent identifiers and provenance.

A positive-population barangay b contributes an origin point x_b and census weight P_b. The available geometry policy constructs a **point inside its administrative polygon** (not a population centroid, building address or residential sample). The OCHA geometries (when explicitly chosen or used for a first run) may reflect an earlier code vintage; unmatched current PSGC barangays are **audited and left unmodeled** unless an explicit compatible alternate geometry is selected. `--geometry-source auto` explicitly permits the OCHA-to-Faeldon fallback. User-supplied points can override both providers.

Each point is snapped to its nearest selected **road node** within 25 km where possible, otherwise to a nearby node of any kind under the existing fallback policy. The final assigned node, connector distance, and validity are recorded. Unsnappable or missing-geometry barangays do not acquire invented routes and are **still counted in the full population denominator**. Thus, origin coverage and demonstrated reachability must not be conflated.

All people in a barangay are represented by **one snapped origin**. This is an explicit approximation: a real barangay may contain road segments in multiple disconnected graph components. v53 polygon/network checks demonstrate that possibility; they do not determine each resident's actual road attachment. No gridded population or dasymetric dataset is currently required or applied.

## 3. Road/ferry graph and shortest paths

The graph contains road and mapped ferry polyline segments, weighted by geographic length (km). Edges are **undirected**, and ferry-polyline *endpoints*, rather than arbitrary mid-route shape points, are candidates for terminal-to-road gangways. The graph reduction preserves selected nodes and constructed-graph shortest paths; an independent sampled verifier compares reduced and original-graph results. These tests do not certify ferry timetables, legal road access, or a fully correct physical network. See [transport](transport.md), [legacy graph methodology](methodology.md), and [validation](validation.md).

Let d_G(u,v) be the nonnegative shortest-path distance between two attached graph nodes. If they are disconnected in the constructed graph, d_G(u,v) = infinity. The location-allocation and OD stages respect these components; no new edges are created merely to make all residents reachable.

## 4. Connected-component-aware network k-medians

Multiple barangays that snap to the same network vertex within a town are combined **by summing their population**, without dropping their original snap-offset contributions from representation diagnostics. Let V_c be the distinct weighted demand vertices within connected component c of a town, and p_i the population at demand vertex i. Candidate medoids are **these vertices**, not arbitrary points on graph edges.

For a chosen medoid set S_c, the familiar *discrete network p-median* access cost is

\[
J_c(S_c)=\sum_{i\in V_c}p_i\min_{m\in S_c}d_G(i,m).
\]

The **k=1** solution is exact among the allowed demand vertices; larger k candidate sets use deterministic greedy addition followed by up to three 1-swap improvement passes. They are **heuristics**, not guaranteed globally optimal k-medians. The system initializes at least one site for **each populated network component** and refuses when a town has more populated components than `max-sites-per-town`.

For the default `--median-selection balanced`, let C be the number of populated components, n the total number of distinct demand vertices across them, K = sum_c k_c, and J_c(k_c) the **computed heuristic** cost curve for component c. Subject to C <= K <= min(n, `max-sites-per-town`), the program chooses among the evaluated allocations to minimize

\[
L(K)=\frac{\sum_cJ_c(k_c)}{\sum_cJ_c(1)}+\frac{K-C}{n-C}.
\]

When a denominator is zero, the implementation handles that case explicitly and prefers smaller feasible K on ties. The allocation uses dynamic programming over computed component curves and a deterministic tie-break. This is a **project-defined normalized access-versus-representative-count criterion**; equal weights on the two terms are a modeling choice. **It is not a theorem, a published canonical k-selector, or a universally optimal k.** Classical network-median models and local-search heuristics underpin the cost curves, not this particular balancing rule ([Hakimi 1964, 1965; Teitz & Bart 1968](literature.md)).

Each demand vertex is assigned to its closest chosen medoid **within the same connected component**. The representative's population weight is the sum of the assigned demand populations. Per-site mean access diagnostics include network access from assigned demand vertices and their saved origin-to-road offsets; those access quantities **are not added to the OD distance matrix**.

## 5. Exactly what the town matrix measures

Let S_A and S_B be the selected medoid sites for towns A and B, with assigned census-derived masses p_s and p_t; let I_st be 1 when d_G(s,t) is finite and 0 otherwise. The code first accumulates the **unnormalized** population-pair totals

\[
N_{AB}=\sum_{s\in S_A}\sum_{t\in S_B}p_sp_tI_{st}\,d_G(s,t),
\qquad
R_{AB}=\sum_{s\in S_A}\sum_{t\in S_B}p_sp_tI_{st},
\]

where disconnected terms contribute zero to N and R, rather than evaluating an undefined 0-times-infinity expression. The distance matrix is

\[
\boxed{D_{AB}=N_{AB}/R_{AB}\quad\text{if }R_{AB}>0;\qquad D_{AB}=+\infty\text{ otherwise}.}
\]

**D is the conditional, weighted average shortest-path distance between modeled *representative graph nodes* when a modeled route exists.** It is **not** the minimum town-to-town distance; **not** centroid-to-centroid distance; **not** a resident-to-resident trip expectation; and **not** a travel-time or schedule estimate. The stored primary D **excludes** original barangay-to-road connectors and within-town origin-to-median access. Those are summarized separately for interpretation and approximation diagnostics.

Let P_A and P_B be the **full census** population totals (including origins missing from the model). The companion reachability quantity is

\[
\boxed{Q_{AB}=R_{AB}/(P_AP_B)}
\]

for positive population totals, otherwise zero. `reachable_pair_probability.npy` is the fraction of all census population-pair mass with a **demonstrated modeled route**; unmodeled population remains in the denominator. It therefore should **not** be read as a measured real-world travel probability or as proof that all unmatched residents are disconnected.

The matrix is symmetric because the road/ferry graph is undirected. **Within-town diagonals may be positive**: they represent two independent selections of the town's population-weighted medoid sites, not the conventional zero cost of a single place to itself. For pairs with Q=0, D is `inf`; CSV blank cells do not carry Q or its cause. Always load the reachability file.

## 6. Upper levels are aggregations, not fresh routing

For an administrative unit U made of towns, population is P_U = sum_{i in U} P_i. The code sums **town-level N and R**, then divides:

\[
N_{UV}=\sum_{i\in U}\sum_{j\in V}N_{ij},\quad
R_{UV}=\sum_{i\in U}\sum_{j\in V}R_{ij},\quad
D_{UV}=N_{UV}/R_{UV}\quad(R_{UV}>0).
\]

Reachability again uses full P_U P_V. **Never average the already-divided D_ij values without their reachable population-pair weights.** The same original barangay population ultimately underlies town, province-equivalent, and region matrices.

`--level province` now denotes a **province-equivalent administrative layer** (`province_equivalent/`): an official province contains its documented member towns; each city or municipality **without a province parent** becomes its own peer OD unit. This includes Pateros and SGA municipalities; province-less cities are labeled `nonprovincial_city`, **not guessed** into HUC/ICC legal categories because the normalized hierarchy does not include that distinction. An explicit user-provided `--province-crosswalk` can assign a province-less town to a registered province, with validation. Every town maps to exactly one upper unit; its population is not omitted. An old `province/` matrix may still exist but is a different, historically incomplete product.

## 7. Artifacts, checkpoint integrity and uncertainty

Population-weighted outputs live under `~/Documents/net-distance-ph/variants/population_weighted/output/<routing-hash-prefix>/<level>/`, with `<level>` one of `town`, `province_equivalent`, or `region`. `latest_run.json` records the requested levels, not every level ever computed. The same route hash can have different exported subsets; selecting an upper-level output can reuse completed town OD totals.

| Artifact | Interpretation |
| --- | --- |
| `distance_matrix_km.npy`, `.csv` | Conditional representative-site network distance (km), with `inf` for no demonstrated route; CSV renders nonfinite cells blank |
| `reachable_pair_probability.npy` | Demonstrated reachable **census-pair fraction**, Q |
| `population_per_unit.npy` | Full input census population per unit |
| `modeled_population_per_unit.npy` | Population successfully represented by a snapped origin |
| `origin_population_coverage.npy` | Modeled/full origin population ratio |
| `mean_representation_access_km.npy` | Mean demand-to-site road access **including origin snap offsets** |
| `distance_approximation_bound_km.npy` | Code-computed approximation diagnostic using mean access and Q; **not** an externally validated real-trip error bound |
| `units.csv` | Row order, PSGC, name, population, and for province equivalents `unit_type` |
| `manifest.json` | Level, definition, source hashes, coverage, route and core fingerprints |

`representative_sites.csv`, selection-specific `selection_audit.csv`, `selection_policy.json`, origin `snap_audit.csv`, and geometry coverage audits document the approximations. Checkpoints are keyed by input/geometry/selection/routing identity and must not be mixed. See [outputs](outputs.md) and [reproducibility](reproducibility.md).

## 8. Scope and scientific reporting

The result is a **plausible model-based geographic network OD matrix**, suitable for studies requiring reproducible relative road/ferry distances under stated assumptions. It does not demonstrate physical access to private or closed roads, live ferry service, or population within a barangay. The model intentionally **does not use dasymetric population grids or modify existing graph topology to cure apparent disconnections**. A six-barangay read-only consistency study showed that the nearest snapped node may be in a small component while major road components also intersect the polygon; these observations document a limitation, not proof of an incorrect assignment.

**Report separately:** (1) the transport and geometry source/vintage; (2) the medoid selection mode and site cap; (3) the conditional distance matrix; (4) reachability and origin coverage; (5) disconnected network pairs and known approximation limits. See [literature](literature.md) for the distinction between canonical methods and the project's own representation policy.

## 8. v56 output discovery and one-run default

The default `--level all` computes the same town-site routing once, then writes **town**, **province-equivalent** and **region** matrices from its saved population-pair totals. The CLI prints full paths to `distance_matrix_km.csv`, `distance_matrix_km.npy`, `reachable_pair_probability.npy`, `units.csv`, and the `latest_run.json` pointer for each requested tier. Use `--level town`, `--level province`, or `--level region` to export only a particular tier. `--geometry-source latest` selects only the exact hash-verified local points used by the most recent completed run, never a guessed replacement.
