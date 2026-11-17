# Population-weighted representative-site routing

This model is **optional** and does not alter the original centroid matrix or its validated transport network.

The command is `net-distance-ph population-weighted`. Use `--level town|province|region|all` (default `all`). The core files are isolated in `variants/population_weighted`.

## Data

All administrative identities, parent relationships, and 2024 barangay populations are read from flat `units.csv` and `barangay_population.csv`; provider-specific source files are not required during a run. The optional `geometry_crosswalk.csv` contains current `psgc_code` and historical `ocha_pcode` columns for a traceable spatial join to older OCHA boundaries. User datasets can provide their own code crosswalk and point coordinates.

## Geometry providers

The **default `net-distance-ph population-weighted` uses OCHA only**, obtained from the official HDX package API. The COD-AB layer largely reflects 2023 barangay boundaries and p-codes. Unmatched 2026 barangays are **audited and skipped by default**, with their full census population retained in town/province/region coverage denominators (no faeldon geometry is downloaded implicitly). Set `--missing-geometry-policy error` only when strict geometry completeness is required. Explicit `--geometry-source auto` opts into OCHA-first with faeldon fallback for unmatched codes or OCHA acquisition failures. The source of each chosen point is recorded in `geometry/*_source_audit.csv`; its ZIP URL and SHA-256 are recorded in the adjacent JSON. Geometries are **representative points on polygons**, not population centers. A direct `--points` or `barangay_points.csv` override always wins.

The faeldon source is a community-maintained derivative of PSA/NAMRIA/OCHA geometry, not legally certified 2026 polygon survey data. Users should compare representative origins against known settlements before inferring physical accessibility. The baseline centroid model is not silently changed.

## Snap coverage and treatment of unknowns

The original default behavior stopped whenever one of 41,998 positive-population barangays could not be attached to the existing road/ferry graph within 25 km. The optional v4 default retains those observations in `cores/<hash>/snap_audit.csv`, excludes them from graph routing without creating new links, and keeps their census population in all output denominator calculations. They are **unmodeled**, not known unreachable. The CLI can restore strict behavior with `--unsnappable-policy error`. Do not enlarge the snap limit automatically.

`population_per_unit.npy` is the complete population represented by the input tables in the specified administrative level. `modeled_population_per_unit.npy` contains only successfully snapped barangay population. `origin_population_coverage.npy` is their ratio. `reachable_pair_probability.npy` is a **lower bound on demonstrated pairwise road/ferry reachability**, with unsnappable population still present in the denominator. No unmodeled route is assigned a fabricated distance.

## Distances

Medoids minimize population-weighted shortest-path access within each populated connected component of each town. **The default is now automatic component-wise k-selection**, chosen by a transparent balanced normalized rate–distortion rule. To reproduce the previous release use `--median-selection one`, which selects exactly one weighted network 1-median per populated connected component. An explicit `--max-mean-access-km` invokes the prior deterministic greedy + 1-swap access-budget method (cannot combine with `--median-selection one`). The total per-town cap remains `--max-sites-per-town` (default 12), not 12 per component. Province and region outputs aggregate population-pair totals from town sites; the population weights always originate in barangay records. Distances are *conditional means among reachable represented site pairs*, and within-unit diagonals need not be zero. Representation-error bounds refer to the **modeled, snapped origin population**, not to missing origin locations or the accuracy of the underlying OSM topology.

The route core preserves all valid barangay terminal nodes separately from the baseline centroid core. A sampled verifier compares site distances to the original, unreduced graph. OSM graph connectivity and origin geography are scientifically independent sources of uncertainty.

Use `net-distance-ph population-weighted --geometry-source faeldon` to force the checksum-verified fallback when HDX access or disk space is problematic. This does not change any existing baseline output.

## Incomplete administrative geometry (v4.2)

By default, `net-distance-ph population-weighted` continues when some OCHA barangay
polygons cannot be reconciled to the chosen plain population dataset. It writes:

- `variants/population_weighted/geometry/*_source_audit.csv`: one entry per
  barangay in the administrative dataset, including `missing_geometry` status;
- `variants/population_weighted/geometry/missing_origins_*.csv`: all **positive-population**
  unmatched barangays with their town and population;
- each output `manifest.json`: `missing_geometry_barangays`,
  `missing_geometry_population`, and the audit path.

The missing population remains part of `population_per_unit.npy` but not
`modeled_population_per_unit.npy`. The reachability-probability denominator
uses the **full** population, treating missing geometry as *unmodeled*, not
as a confirmed disconnection. Municipalities without any routable origins
remain in the output with infinite distance and zero reachability probability.
Only a complete absence of any usable positive-population origin is fatal.

`--missing-geometry-policy error` restores the completeness requirement.
OCHA download errors and malformed datasets are still errors, not "missing
coverage"; there is no automatic faeldon fallback.


## Adaptive component-wise k-median selection (v4.3)

Default `--median-selection balanced` selects **k separately for every populated
connected component**, while enforcing a **single maximum count per town**.
For each component `c`, `J_c(k)` is the population-weighted total shortest-path
access to its nearest `k` medoids, among candidate snapped barangay nodes. The
exact one-median starts each component's cost curve. Larger `k` use deterministic
greedy augmentation and 1-swap refinement; these are **heuristics** for the
NP-hard discrete k-median optimization, not exact global solvers.

The automatic policy minimizes across evaluated site allocations:

    score = [sum_c J_c(k_c) / sum_c J_c(1)]
          + [sum_c (k_c - 1) / sum_c (n_c - 1)]

where `n_c` is the number of distinct positive-population snapped demand nodes
in that component. The first fraction measures *remaining road-access
representation distortion* relative to the one-median baseline. The second
fraction measures *additional representative sites* as a fraction of all
optional demand nodes. In degenerate cases (no extra demand nodes or zero
baseline distortion), the smallest feasible k is selected. Both terms are
normalized, so there is **no user-supplied penalty or kilometer threshold**.
Equal importance of these two ratios is an explicit modeling assumption;
this criterion yields an optimal k **for this specified tradeoff and the
computed heuristic cost curves**, not a universally optimal statistical k.

Population assignment to chosen sites, the town-to-town conditional
population-weighted mean of reachable representative shortest paths, reachability
probabilities and **all population denominators remain unchanged**. The policy
adds no geographic links or origin points. It is *not* the minimum site-to-site
distance and is not the exact expected distance between individual residents.

A fresh selection-specific checkpoint is written under
`variants/population_weighted/town_sites/<core-key>/<selection-key>/`, including
`selection_audit.csv` (town, demand-node count, components, medians, component
allocations, mean access, and score) and `selection_policy.json`. This makes
choices reproducible and prevents mixing old one-median selection checkpoints
with adaptive selection. Existing national graphs, barangay cores, and earlier
published output matrices remain intact. New representative-site sets use a
new routing fingerprint; the existing routing checkpoint may be reused *only*
when the exact representative sites and weights are identical.

**Commands:**

    net-distance-ph population-weighted --geometry-source faeldon
    net-distance-ph population-weighted --geometry-source faeldon --median-selection one
    net-distance-ph population-weighted --geometry-source faeldon --max-mean-access-km 10

Use the second command to reproduce the old default, and the third to apply a
*user-specified* kilometer error budget rather than automatic rate–distortion
selection. These are scientifically distinct modeling choices and should be
compared rather than conflated.
