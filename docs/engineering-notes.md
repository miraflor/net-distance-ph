# Engineering notes from the original implementation

!!! note "Historical implementation notes"
    This page preserves the original project's deep technical explanation and historical
    local benchmarks for auditability. Statements about absolute paths, bundled output,
    and setup in the original document are superseded by the current documentation.
    Performance measurements are historical, not guarantees for a new source snapshot.

# Road + ferry distance matrix for Philippine municipalities

Shortest-path distance, in kilometres, between every pair of the 1,642
municipal/city centroids, over a single network combining the Geofabrik road
extract with the OSM ferry routes.

This patched version includes three targeted correctness/operability fixes:

1. Stage 2 has timestamped progress, heartbeats, a persistent log, and atomic resumable checkpoints.
2. On Windows, NumPy memory maps are explicitly closed so temporary test files are not left locked.
3. Automatic road connections are created only from ferry-polyline endpoints, not intermediate ferry shape points that merely pass near the coast.

```
pip install numpy scipy          # that is the whole dependency list
# optional: pip install pyproj   # exact Karney geodesics instead of Lambert

python run.py all --workers 8
```

Point `DATA` in `config.py` at wherever you unpacked the Drive folder. The
three layers are found by filename anywhere underneath, so the
`primsectertrunk_road_shp/`, `ferry_route_shape_shp/`, `muncity_centroids_shp/`
layout works unchanged.

```
python run.py graph                  # stages can also be run one at a time
python run.py reduce                 # resumable between contraction passes
python run.py matrix --workers 8     # resumable: just rerun after a crash
python run.py verify
```

| flag | meaning | default |
|---|---|---|
| `--workers N` | worker processes | cores − 1 |
| `--batch N` | sources per Dijkstra call | 8 |
| `--waves 1,2` | Euclidean neighbour orders to prioritise; `--waves ""` disables | `1,2` |
| `--wave-scale F` | inflate wave radii by F | 1.5 |
| `--no-partial` | don't rewrite `out/` after every wave | off |

Outputs in `out/`:

| file | contents |
|---|---|
| `distance_matrix_km.npy` | (1642, 1642) float64; row order = `towns.csv` |
| `distance_matrix_km.csv` | same, PSGC codes as headers; empty cell = not resolved |
| `row_validity_radius_km.npy` | per-row radius — see below |
| `towns.csv` | row index, PSGC, name, centroid lon/lat, how it attached, row radius |
| `matrix_report.json` | asymmetry, pairs resolved, rows complete |
| `verification.txt` | the checks from stage 4 |

---

## Interruption safety

### Stage 2: core reduction

Stage 2 now prints a timestamped heartbeat every `REDUCE_HEARTBEAT_SECONDS`
(default 30 seconds), mirrors its output to `work/reduce_progress.log`, and
writes `work/reduce_checkpoint.npz` atomically after every
`REDUCE_CHECKPOINT_EVERY_PASSES` completed contraction passes (default 1).
Rerunning `python run.py reduce` resumes automatically. An interruption during
a pass repeats only that unfinished pass; the graph is checkpointed only after
a pass is internally complete. Use `python check_reduce_status.py` from a
second terminal to inspect the last heartbeat and saved state. Use
`python run.py reduce --restart-reduce` to discard the checkpoint deliberately.

A sleeping laptop pauses the process. It does not create progress while asleep;
a gap in the timestamped heartbeat shows exactly when execution paused and
resumed.

### Stage 3: matrix calculation

Nothing is ever lost. The matrix is a memory-mapped `.npy`, so rows reach disk
as they are absorbed; the per-row validity radius is saved separately and
replaced **atomically**, and only *after* the matrix is flushed. A crash
between the two leaves rows that are better than their recorded radius claims,
which is the safe direction — they are simply recomputed. Checkpoints land
every `CHECKPOINT_EVERY` batches (default 8, so ~64 rows) and always at the end
of a wave.

`out/` is rewritten after every wave, so even mid-run the deliverable on disk
is a real, usable, correctly-labelled matrix. To resume, rerun
`python run.py matrix`. To start over, delete `work/core_matrix.npy` and
`work/core_radius.npy`.

## Reading a partial matrix

The rule is on one line:

> **Row `i` with validity radius `R`: every finite entry is exact and final;
> every infinite entry is a pair known to be further apart than `R`.**

When `R` is infinite the row is complete, and infinity then means genuinely
unreachable. `row_validity_radius_km.npy` and the `validity_radius_km` column
of `towns.csv` carry this, so a half-finished matrix is never ambiguous: it is
never *wrong*, only incomplete, and it tells you exactly where.

---

## Neighbour priority

Sources are visited in breadth-first order over the Euclidean k-nearest-
neighbour graph of the centroids, and the matrix is filled in **waves** of
increasing radius.

Wave *t* runs Dijkstra with a distance limit derived from each town's *t*-th
order neighbourhood in that kNN graph — order 1 is the first neighbour ring,
order 2 is neighbours of neighbours — and the final wave lifts the limit
entirely. A radius-limited Dijkstra settles only the nodes inside the radius,
which is why the early waves are nearly free.

Measured on a synthetic core of the same shape as the real one (37,461 nodes,
176k edges, 1,642 sources, single core):

```
no waves                     13.4 s
waves (1,2) at scale 1.5     15.5 s      1.16x
  wave 1      done at  0.8 s   ( 5% of total)   46,029 pairs   ~28 towns/row
  wave 2      done at  2.0 s   (13% of total)  145,197 pairs
  unlimited   done at 15.5 s                 2,696,164 pairs
final matrices identical, entry for entry
```

So the whole first neighbour ring — comfortably more than the *k* any kNN
weights matrix would want — is on disk after 5% of the runtime, for a 16%
premium on the total. Set `--waves ""` to pay nothing and get no early output.

**One thing to be precise about**, because it's easy to assume more than is
true. Network distance always exceeds straight-line distance, often by a lot
around a bay. A radius `R` derived from Euclidean geometry guarantees this and
only this: *every pair whose network distance is ≤ R is now exact*. It does
**not** guarantee that town *i*'s Euclidean *t*-hop neighbours are resolved —
a town 30 km away as the crow flies can be 200 km by road and will wait for a
later wave. That is why `WAVE_SCALE` defaults to 1.5 (inflating the radius to
absorb typical road detour) and why each wave reports the median number of
towns actually resolved per row. If that number is below what you need, raise
`WAVE_SCALE`; the cost grows roughly with its square.

---

## Is d(X, Y) = d(Y, X)?

Yes, and by construction rather than by luck. The graph is undirected with
non-negative weights, so a shortest path from X to Y is a set of edges whose
total is the same walked backwards. There is no one-way modelling, no
single-direction ferry, no turn penalties — the usual reasons a real road
matrix comes out asymmetric.

Four things could nonetheless break it, and each is handled:

1. **Parallel edges resolved inconsistently.** `dedupe_edges` keeps the minimum,
   once, so both directions see the same weight. This matters more than it
   looks: scipy's COO→CSR conversion *sums* duplicate entries, so an
   undeduplicated parallel edge silently becomes an edge of the wrong weight.
2. **The centroid stub charged at one end only.** It is added at *both* ends.
3. **Mid-run asymmetry from the waves.** Rows *i* and *j* can briefly hold
   different validity radii, so *j* may be resolved from *i* but not yet the
   reverse. The elementwise minimum with the transpose repairs this — and it is
   a repair, not a fudge, because a finite entry is always final.
4. **Floating-point summation order.** Dijkstra from X adds path weights in one
   order and from Y in the reverse, and float addition is not associative. This
   is the only irreducible residue and it is tiny: 3e-16 relative, the last
   bit. It is reported in `matrix_report.json` before being removed. If it ever
   exceeds ~1e-12 relative, something upstream is wrong and stage 4 will say so.
   *(Correction, 2026-11: the original stage 4 checked symmetry only after the
   matrix had been made symmetric, so it could not fail. It now reads the
   pre-symmetrisation value and fails above `VERIFY_MAX_ASYMMETRY_RELATIVE`.)*

**Symmetry buys no speed, and it is worth being clear why**, because the
"compute only the upper triangle" intuition is strong. The unit of work is not
a pair, it is a *source*: one Dijkstra from X settles every node in range and
therefore produces the entire row X at once. Row X is also column X by
symmetry — but you still need all 1,642 rows, and there is no way to get a row
for less than one Dijkstra. Halving the pairs does not halve the rows. Symmetry
is used here as a **correctness check** instead.

---

## Optimisations

Everything below was measured on this workload, not assumed. Timings are
single-core; parallelism multiplies on top of all of them.

### Adopted

| | effect |
|---|---|
| **Node contraction to a core** | **66×** — the dominant win, see below |
| **RCM node reordering** | **1.44×** — pure cache locality, changes no distance |
| **Radius-limited waves** | near pairs at 5% of runtime, 1.16× total cost |
| **int32 CSR indices** | 1.05× — the layout scipy wants, so no per-call conversion |
| **Drop source-free components** | small, free — islands no town snaps to leave the CSR |
| **De-duplicate sources** | towns sharing a core node are computed once |
| **Memory-mapped core in workers** | N workers share one copy of the graph in RAM |
| **No geopandas** | reading 126k ways directly in numpy beats building shapely geometry |
| **Parallel sources** | ×(cores), the smallest of the wins but free |

**Node contraction** is where the runtime actually goes. The raw noded graph
has millions of nodes but only ~1,642 are ever a source or target; the rest are
OSM shape points recording road curvature, and Dijkstra costs O(E + V log V)
per source, so they are pure tax. So we remove a non-source node *v* and, for
every pair of its neighbours (a, b), insert a shortcut of weight
w(a,v) + w(v,b) — the preprocessing step of Contraction Hierarchies
(Geisberger et al. 2008), run until only a core remains.

It is exact in both directions. No distance can **decrease**: every shortcut is
an actual path a→v→b, so its weight is achievable, never a shorter fiction. No
distance can **increase**: any original shortest path using *v* as an interior
node enters and leaves via two incident edges, and that pair is exactly the
shortcut inserted; *v* can never be a query endpoint because sources are never
contracted. Degree 1 is leaf pruning (no shortcut — a leaf cannot be interior
to any path) and degree 2 is chain contraction (one shortcut); they are the
same operation at different degrees, so there is one routine. Contraction runs
over an *independent set* of nodes per pass, fully vectorised.

On a synthetic network of the same shape and roughly twice the size of the real
one (6.2M nodes, 6.4M edges, 1,642 sources):

```
reduction        6,186,263 -> 52,562 nodes (0.8%), 6.4M -> 262k edges    138 s, once
Dijkstra, full   1100 ms/source  ->  30.1 min
Dijkstra, core     16.6 ms/source ->   0.5 min                            66x
```

**Accuracy is not traded away for any of this.** Edge weights are ellipsoidal
(Lambert on WGS84, within 1.4 mm per km of Vincenty — spherical haversine would
be biased up to ~0.5% by latitude and bearing and would rescale the whole
matrix). Nearest-neighbour search uses a KD-tree on 3-D ECEF coordinates rather
than a projected CRS: Euclidean distance in ECEF is the chord, a strictly
increasing function of surface distance, so neighbour *ranking* is exact
everywhere with no projection and no distortion at the edges of the country.

### Measured and rejected

* **float32 edge weights.** No measurable gain (1.00×) and it would throw away
  precision for nothing.
* **Per-component subgraph extraction.** The only waste in routing a small
  island's sources against the full CSR is scipy initialising the distance
  array — about 0.3 ms against a 7.6 ms query, ~4% on a minority of rows. Not
  worth the node-remapping complexity.
* **Contracting past degree 2.** Chain contraction does ~99% of the work; a
  sweep over `MAX_CONTRACT_DEGREE` ∈ {2, 4, 6} moved the total by less than the
  measurement noise. Left at 4, configurable.
* **RPHAST / bucket-based CH many-to-many.** Would shave the remaining
  half-minute, and is the right answer if the source count ever grows by an
  order of magnitude. At 1,642 sources it is not worth the complexity or the
  extra surface area for bugs.
* **Upper-triangle-only computation.** Not an optimisation — see the symmetry
  section.

---

## Method, step by step

**1. Noding.** Road and ferry vertices are rounded to 1e-6° (~0.11 m) and equal
coordinates become the same node. OSM ways genuinely share vertex coordinates
at junctions, so coincident vertices *are* the junction; the rounding heals
float noise from the shapefile round-trip without welding together
grade-separated crossings, which OSM digitises as distinct nodes metres apart
in plan. Multipart polylines are split at part boundaries, so no phantom edge
is created between the end of one part and the start of the next.

**2. Ferries.** Roads and ferries pass through the same noder, so chained ferry
legs and terminals sitting on a road vertex connect automatically. Terminals
digitised offshore get a *gangway* edge to the nearest road node within
`FERRY_CONNECT_KM` (default 5 km), weighted by its own length — not a free
teleport. Ferry legs cost their geodesic length by default; set
`FERRY_COST_MULTIPLIER` / `FERRY_FIXED_PENALTY_KM` to turn the output into a
generalised cost that prices in the slowness and waiting of a sea crossing.

**3. Centroids.** Each centroid attaches to the nearest *road* node; failing
that, the nearest node of any kind (for a roadless islet that is its ferry
terminal, which is correct); failing that within `MAX_CENTROID_SNAP_KM`, it is
reported unsnappable rather than silently attached to something implausible.
*(Correction, 2026-11: the nearest ferry node is not always a terminal; it
can be a mid-route shape point of a passing route. See
`SNAP_FALLBACK_TERMINALS_ONLY` in `config.py`.)*

**4. Contraction, RCM reordering, then waves of Dijkstra**, as above.

---

## Verification

`python run.py verify` writes `out/verification.txt`:

1. Exact symmetry of both the values and the resolved/unresolved pattern
   (and, since 2026-11, the pre-symmetrisation residual from `matrix_report.json`).
2. Non-negativity and a zero diagonal.
3. Triangle inequality on 200,000 random triples (a failure since 2026-11; see the correction below).
4. **Re-runs `VERIFY_N_SOURCES` complete rows against the unreduced graph** and
   compares. This is the one that matters — a direct empirical check that
   contraction, RCM and the waves changed nothing. It should come out ~1e-13 km.

The test suite covers the same properties where brute force is affordable:

```
python tests/test_geo.py          # Lambert vs Vincenty across six length scales
python tests/test_shpio.py        # shapefile round-trips, multipart, PSGC leading zeros
python tests/test_reduce.py       # reduced core == full graph to 4e-16, six graph families
python tests/test_matrix.py       # waves == no waves; partial-state soundness; crash/resume
python tests/test_end_to_end.py   # whole pipeline on a synthetic archipelago
```

`test_matrix.py` includes the invariant the wave scheme rests on: after a
partial wave, every finite entry must equal the truth and no pair inside the
row's radius may be missing. It runs against a state where only 20% of pairs
are resolved, so it is actually exercising the partial case.

---

## Things to check before trusting the output

**Unresolved pairs.** OSM ferry coverage in the Philippines is uneven. Major
RORO and inter-island routes are generally mapped; smaller municipal routes
often are not. An island whose only link is an unmapped ferry comes out
infinite — correctly given the input, not necessarily given reality. Check
`matrix_report.json` against expectation; a large count means the ferry layer,
not the code, needs work. Note the distinction the validity radius draws:
infinite-and-complete is unreachable, infinite-and-incomplete is just not done
yet.

**`snap_tier` in `towns.csv`.** Tier 0 (road) is normal. Tier 1 means the town
reached the network through a ferry node — plausible for a small island,
suspicious for a mainland town. Tier −1 means it never attached and its row is
entirely infinite.

**`INCLUDE_SNAP_OFFSET`.** On by default: the straight-line stub from centroid
to network is added at both ends, which keeps co-located towns from showing
exactly zero and makes the matrix a better door-to-door estimate. It is
symmetric so it cannot break d(X,Y) = d(Y,X), but it *can* produce small
triangle-inequality violations, because a two-leg trip pays the intermediate
town's stub twice. If you need a strict metric — some spatial-weights
constructions do — set it to `False` for pure node-to-node network distance.

*(Correction, 2026-11: the triangle-inequality statement above is wrong.
Paying the intermediate stub twice makes the two-leg trip longer, which can
only add slack, so the inequality holds with the stubs included. The matrix
is a metric either way; `INCLUDE_SNAP_OFFSET = False` is a modelling choice,
not a requirement for a metric.)*
