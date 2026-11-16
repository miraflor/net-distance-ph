# Unified command-line interface

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
