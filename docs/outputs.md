# Output contract and interpretation

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

## Historical example, not a bundled dataset

The original completed archive's `matrix_report.json` reported:

| Statistic | Original run |
| --- | ---: |
| Number of towns | 1,642 |
| Snapped towns | 1,638 |
| Completed rows | 1,642 |
| Finite ordered matrix cells | 2,515,500 |
| Infinite ordered matrix cells | 180,664 |
| Largest finite entry | 2,198.027 km |
| Max presymmetrization asymmetry | 2.73e-12 km |

The counts and values are recorded for transparency, **not regenerated** by
the GitHub-ready source package. The archived report did not include enough
provenance metadata to independently verify its input snapshot or licensing.

## Important statistical distinctions

`D` is symmetric because the model is undirected, not because roads and ferries
are genuinely bidirectional. Its values are **geometry-weighted shortest paths**,
not observed distances traveled by individual vehicles. The presence of a link
in the graph means the extracted geometry connects, not necessarily that a
person can traverse it on a given date or at a given time.
