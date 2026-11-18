# Output contract and interpretation

<!-- v55-methods-documentation -->

**The main section below documents the separate legacy centroid outputs.** The default `net-distance-ph population-weighted` outputs are documented under [Primary population-weighted OD output](#primary-population-weighted-od-output-distinct-from-the-centroid-out-files-above). Their distance and diagonal definitions are different.

The program creates files in `out/` (or `--out`). **The row order is determined
by the input locality point shapefile**, never by PSGC numerical sorting.

## Deliverables

| File | Format | Description |
| --- | --- | --- |
| `distance_matrix_km.npy` | NumPy `float64[N,N]` | Network distance in kilometres, or infinity |
| `distance_matrix_km.csv` | CSV | Same matrix with text locality IDs as column labels and index |
| `towns.csv` | CSV | `row,id,name,lon,lat,snap_tier,snap_offset_km,validity_radius_km` |
| `row_validity_radius_km.npy` | NumPy `float64[N]` | Per-row distance threshold for computed paths |
| `matrix_report.json` | JSON | Completeness, reachability and asymmetry statistics; `stage`, `include_snap_offset`, and the `core_fingerprint` of the graph it came from |
| `source_manifest.json` | JSON | Input shapefile SHA-256 identities, graph build settings, and reduced core fingerprint |
| `verification.txt` | text | Direct numerical and graph checks, one `PASS`/`FAIL` line each, and an overall `RESULT` |

CSV values are rounded to **four decimals of a kilometre**, while `.npy`
retains full float64 precision. A CSV empty cell encodes **both** incomplete
and unreachable cases; consult the radius and stage report before using it.
Both CSV files are standard CSV (RFC 4180 quoting): a field is quoted only if
it contains a comma, quote or line break, so read them with a CSV parser
rather than by splitting on commas.

## Read a cell correctly

Given locality IDs `A`, `B` and matrix row `i`:

| Matrix entry | Row radius | Interpretation |
| --- | --- | --- |
| finite number | any | Final computed network distance |
| `inf` | finite | Not yet settled within the row's current radius; final reachability unknown |
| `inf` | `inf` | Unreachable *on the modeled graph* |
| zero diagonal | any | Defined distance of a locality to itself |

Finiteness of a row radius is different from finiteness of a matrix value.
The `n_rows_complete` field counts rows whose radius is infinity.

## Example: inspect by ID

```python
import csv
from pathlib import Path
import numpy as np

output = Path("out")
with (output / "towns.csv").open(encoding="utf-8", newline="") as f:
    towns = list(csv.DictReader(f))

ids = [town["id"] for town in towns]
assert len(ids) == len(set(ids)), "IDs must be unique"
index = {code: row for row, code in enumerate(ids)}
distance = np.load(output / "distance_matrix_km.npy", mmap_mode="r")
radius = np.load(output / "row_validity_radius_km.npy")

origin_id = ids[0]
destination_id = ids[1]
i, j = index[origin_id], index[destination_id]
value = distance[i, j]

if np.isfinite(value):
    print(f"{origin_id} to {destination_id}: {value:.3f} km")
elif np.isinf(radius[i]):
    print("No route exists in the supplied graph")
else:
    print("This pair was not yet resolved; resume the matrix stage")
```

## Snap diagnostics

`towns.csv` contains:

- `snap_tier = 0`: attached to a road node.
- `snap_tier = 1`: attached to a node in the full network after no qualifying
  road attachment (a ferry node when road nodes were tried first; by default
  possibly a mid-route shape point, see `SNAP_FALLBACK_TERMINALS_ONLY`).
- `snap_tier = -1`: no node within the configured snap radius; the locality
  cannot participate in reachable paths.
- `snap_offset_km`: the connector from the input point to its attached node,
  included at both ends by default.

Large offsets, unresolved islands, and ferry-node snaps should be individually
audited before using the matrix as a transport proxy.

## National results

No completed nationwide matrix or route reachability report is published by this repository. Any future numerical result must name its source snapshot and run metadata.

## Important statistical distinctions

`D` is symmetric because the model is undirected, not because roads and ferries
are genuinely bidirectional. Its values are **geometry-weighted shortest paths**,
not observed distances traveled by individual vehicles. The presence of a link
in the graph means the extracted geometry connects, not necessarily that a
person can traverse it on a given date or at a given time.

## Primary population-weighted OD output (distinct from the centroid `out/` files above)

The installed default `net-distance-ph population-weighted` exports **all three levels** to:

```text
~/Documents/net-distance-ph/variants/population_weighted/output/YYYY-MM-DD_HH-MM-SS/town/
```

The default additionally produces `province_equivalent/` (not the historical `province/`) and `region/`. `--level town|province|region` can select just one. These exports share **town-level routing totals**, with no separate higher-level medoid selection. `latest_run.json` describes only the levels requested in the latest invocation.

| File in each population-weighted level | Meaning |
| --- | --- |
| `distance_matrix_km.npy` / `.csv` | **Conditional** weighted mean of shortest **representative road-node** distances for site pairs with a route, in km; `inf` if no pairs are reachable; CSV renders nonfinite as blank |
| `reachable_pair_probability.npy` | Modeled reachable representative population-pair mass divided by **full** census population-pair mass (including unmodeled demand) |
| `population_per_unit.npy` | Complete input population at that administrative level |
| `modeled_population_per_unit.npy` | Population with successfully snapped and represented origins |
| `origin_population_coverage.npy` | Modeled / full population |
| `mean_representation_access_km.npy` | Mean population-weighted distance from original origin to selected representative, including origin-to-road snap offsets |
| `distance_approximation_bound_km.npy` | Model-computed representation-error diagnostic, **not** a verified real-world trip-distance error bound |
| `units.csv` | Exact row order and PSGC codes; province-equivalent adds `unit_type` (`province`, `nonprovincial_city`, `nonprovincial_municipality`) |
| `manifest.json` | Model, input, graph, routing, and population coverage metadata |

For towns A and B, the code accumulates `N_AB = sum(reachable sites s,t) p_s p_t d_G(s,t)` and `R_AB = sum(reachable sites s,t) p_s p_t`. It reports `D_AB=N_AB/R_AB` when `R_AB>0`, otherwise `inf`. Reachability is `Q_AB=R_AB/(P_A P_B)`, with **full input census** P. At the upper tiers it sums the unnormalized town `N` and `R` first; it does **not** take a simple average of town distances.

**Unlike the legacy centroid matrix**, the population-weighted diagonal may be positive, and `D_AB` **excludes** barangay snap connectors and demand-to-median access. Unmodeled origins are *not* considered proven disconnected. There is no radius-limited incomplete-wave ambiguity in a **finished** population-weighted output; the legacy `row_validity_radius_km.npy` contract applies to the separate centroid pipeline only. See [exact mathematical definitions](population_weighted.md#5-exactly-what-the-town-matrix-measures).

### View and compare published matrices

From the repository environment, launch the local interactive explorer:

```powershell
net-distance-ph view
```

It lists **completed population-weighted output runs** and whichever
`town`, `province_equivalent`, and `region` levels exist in each run. Selecting
an administrative unit highlights the **closest X other finite-distance
units** and ranks their stored `distance_matrix_km.npy` values; the interface
also reads `reachable_pair_probability.npy` when present. It does **not**
create or modify matrices and does **not** infer a finite path from a blank
CSV cell. The selected run and level determine the `units.csv` row order.

Administrative polygons, simplified road classes, and ferries are optional
**display layers** prepared from the currently selected source releases and
cached under `variants/population_weighted/view_cache/`. They may not match
an older matrix's original source releases, and displayed lines are **not
reconstructed origin–destination paths**. See [the viewer CLI instructions](cli.md#local-output-explorer-view).

### v56: where the three population-weighted matrices go

The default `net-distance-ph population-weighted` writes **all three** levels under `~/Documents/net-distance-ph/variants/population_weighted/output/YYYY-MM-DD_HH-MM-SS/`: `town/`, `province_equivalent/`, and `region/`. Each has `distance_matrix_km.csv`, `distance_matrix_km.npy`, `reachable_pair_probability.npy`, `units.csv`, and `manifest.json`. The command **prints the absolute directory and filenames for each level** and a `LATEST OUTPUT INDEX` path. The durable `variants/population_weighted/output/latest_run.json` contains `matrix_outputs` with these absolute paths. No assumption about the current working directory is needed.

The dated directory is created in the machine's **local time** when export begins; if two runs share the same second, a `-001` suffix is added. Existing hash-named output folders are preserved and recognized by audit/geometry-reuse tools. Graph and Dijkstra checkpoint directories remain hash-based.
