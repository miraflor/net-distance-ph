# Read-only graph connectivity diagnostics

After `net-distance-ph run --until connectivity` stops at an incomplete/disconnected network, the stage-1 graph and the full audit remain intact. Inspect graph topology without rebuilding or changing any snap or ferry thresholds:

```powershell
net-distance-ph diagnose-connectivity
```

For a different geographic level, use `net-distance-ph diagnose-connectivity --boundary-level province` (for an existing province-level graph). Use `--top 40` to print more components.

The command performs the same saved-source fingerprint check as the existing connectivity stage. It **does not download OSM data, re-extract roads, re-snap towns, add graph edges, run the matrix stage, or change the connectivity gate**. It reads `work/graph_full.npz` and `work/sources.npz` and produces:

- `output/connectivity_diagnostics/component_gaps.csv`: one row per road-and-ferry component containing towns outside the largest; number of towns, road vertices and edges, and the closest observed pair of **road vertices** between it and the main component.
- `output/connectivity_diagnostics/affected_towns.csv`: all off-main and unsnappable towns, their actual snap offsets and straight-line distances to the main component's nearest road vertex.
- `output/connectivity_diagnostics/component_gap_lines.geojson`: straight-line **diagnostic markers**, not recommended road/ferry links. Load as a layer in QGIS alongside the road/ferry shapefiles.
- `output/connectivity_diagnostics/diagnostic_summary.json`: counts, provenance fingerprint, and interpretation limitations.

**Interpret cautiously:** Distance to the nearest stored vertex is an upper bound on distance to the nearest *road polyline*. If a road intersects another way between vertices, this diagnostic can miss a zero-distance crossing. A small gap between vertices does not justify joining them: the ways may be different bridge levels, separated by water, or lack a legal traversable link. For suspected gaps, examine OSM node IDs and the source `highway` tags/linework. The current road extraction includes `trunk`, `primary`, `secondary`, `tertiary` by default and can omit local roads, motorway ramps and other links. Investigate omissions *before* changing the transport filter or relaxing the graph's 25 km source-snap and 5 km ferry-connector limits.

The first localities to inspect are mainland isolates (Abra's Bucloc/Daguioman/Sallapadan, Zambales' Candelaria/Masinloc, and Badoc/Vintar/other singletons), then the small island groups and the three unsnappable islands. This ranking is an investigation choice, not proof of a defective mapping.
