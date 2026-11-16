# Local OSM examination of graph disconnects (read only)

Use this **after** `net-distance-ph diagnose-connectivity` has produced
`output/connectivity_diagnostics/component_gaps.csv` and a provenance-checked
`diagnostic_summary.json`.

```powershell
net-distance-ph inspect-osm-gaps --top 9 --radius-m 1000
```

This does **not** download data, re-extract transport layers, change the
selected `current_transport.json`, regenerate the graph, or add any edge.
It reads the **cached, SHA-256-verified OSM PBF used in the current transport
release**, and verifies that the gap report refers to the saved graph's
input fingerprint. The command performs a national PyOsmium PBF scan, so it
may take substantial time and memory. A single `locations=True` scan is used
for all selected gaps. Reading a national OSM PBF is not a network download.

For each gap, it examines original `highway=*` ways within 1 km of either
endpoint and tests three *local, undirected* OSM node-ID networks:

1. **Baseline:** exact road classes recorded in the selected transport
   release's provenance (currently `trunk`, `primary`, `secondary`, `tertiary`).
2. **Ordinary expansion:** baseline plus `unclassified`, `residential`,
   `living_street`, `motorway`, and `*_link` classes. Nonpublic added ways with
   `access`, `vehicle`, `motor_vehicle`, or `motorcar` equal to `no` or
   `private` are excluded.
3. **Service sensitivity:** ordinary expansion plus public `service` ways.

Only **identical OSM node IDs** join segments. A geometric crossing or a
metres-long gap **never** becomes an edge. The OSM endpoints are matched to
existing graph gap vertices at the graph's six-decimal coordinate rounding.
The search is restricted to the specified radius around the gap endpoints;
a missing path is inconclusive because paths may leave that window. Road
traffic direction, turn restrictions, bridge levels, ferries, conditional
access, and legal/physical drivability are **not proved** by a found path.
These caveats are recorded in the JSON output and some tags are included in
CSV output.

Outputs: `workspace/output/osm_gap_inspection/`

- `gap_findings.csv`: per-gap observed classes, endpoint matches, local path
  presence, OSM witness way IDs, and cautious verdict.
- `nearby_osm_ways.csv`: nearby OSM way IDs and tags (including excluded
  motorized and nonmotorized classes).
- `local_paths.geojson`: line segments depicting original graph gaps and
  any *local* node-ID path found. No recommended new edges.
- `inspection_summary.json`: PBF SHA-256, original source fingerprint,
  selected baseline road filter, radius, counts, and limitations.

To narrow to the Abra gap for a second investigation:

```powershell
net-distance-ph inspect-osm-gaps --top 1 --radius-m 3000
```

Do **not** infer that `service` roads should all be included in the routing
default, or that a short gap should be snapped closed. First review the
corresponding OSM way IDs and tags and use a separate controlled network
build to quantify the effect of each road-filter change.
