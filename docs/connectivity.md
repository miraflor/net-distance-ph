# Town-by-town connectivity audit

**Status:** This feature is an audit method, not a nationally validated result.

After building your own stage-1 road-and-ferry network, run:

```powershell
python run.py connectivity --verbose
```

The command reads `work/graph_full.npz` and `work/sources.npz` from the
existing `net-distance-ph` workspace, verifies stage-1 input hashes and settings,
and compares snapped towns against the pinned Q2 2026 PSGC roster
(1,642 cities and municipalities). It does **not** download inputs,
construct a graph, contract the graph, run shortest-path searches, or modify
existing stage-1 data.

For a separate command-line executable, install the project in editable mode:

```powershell
python -m pip install -e .
net-distance-ph-connectivity
```

The same audit is available with `python -m net_distance_ph.connectivity`.
Both support `--workspace`, and the standalone form also supports `--repo`,
`--work`, `--out`, `--self-test`, and `--skip-provenance`. With custom paths,
`python run.py connectivity --workspace "D:\MyWorkspace"` uses the same
workspace conventions as the other stages.

## Outputs

The directory `~/Documents/net-distance-ph/output/connectivity_audit/` contains:

- `connectivity_report.json` — overall status, official roster coverage, snap
  counts, component counts, connected/disconnected **unordered** town-pair
  counts, and source provenance
- `town_connectivity.csv` — every official town, including missing or
  unsnappable ones, and the number of other towns it cannot reach
- `component_summary.csv` — components containing official towns, with sizes
  and sample members
- `barrier_examples.csv` — example pairs from disconnected components

## Exit status

| Exit code | Meaning |
| --- | --- |
| `0` | Complete official roster attached to one connected component in the modeled road-and-ferry graph |
| `1` | Audit ran but official coverage, snapping, or modeled connectivity is incomplete |
| `2` | Missing graph, invalid inputs, failed provenance validation, or unchecked diagnostic |

The connectivity audit is cheaper than a full Dijkstra distance matrix.
Connectivity of the *undirected modeled network* does **not** establish a
feasible real-world journey: ferries may be inactive, and road access,
terminal connections, and grade-separated crossings require independent checks.
Road-only results treat towns attached solely to ferry nodes as isolated.
The optional standalone `--skip-provenance` flag can produce diagnostic CSVs,
but it **never** yields a verified PASS.

Recommended workflow: `graph` → `connectivity` → review missing links →
`reduce` → `matrix` → `verify`. To preserve backward compatibility, the
existing `all` command still runs only the original four computational stages;
connectivity must be invoked explicitly.
