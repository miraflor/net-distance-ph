# Running the legacy four-stage centroid pipeline

<!-- v55-methods-documentation -->

**This is the legacy one-reference-point-per-locality graph/matrix runner.** The primary population-weighted method is `net-distance-ph population-weighted` (all three population-weighted matrices, balanced component-aware k-medians by default). It reuses the prepared original graph but builds a separate demand-terminal core and output directory. See [the primary method](population_weighted.md) and [current CLI](cli.md).

The default entry point, `python run.py all`, first prepares missing inputs
from independent upstream sources in `~/Documents/net-distance-ph/data/` and
then executes the four graph/matrix stages below. Use
`python run.py prepare` to acquire and reconcile sources without computing the
matrix. `--workspace` selects a different persistent workspace. If `--data`
is explicitly supplied (or `NET_DISTANCE_PH_DATA` is set), the existing
shapefile workflow is preserved and no automatic acquisition takes place.

All console output is timestamped and copied to the workspace's `logs/`
directory. Each long step emits elapsed-time heartbeat messages; this is
liveness information, not a claim about numerical progress.

## Execution plan

```mermaid
flowchart TD
  A[graph: read inputs and node polylines] --> B[work/graph_full.npz + sources.npz]
  B --> C[reduce: deduplicate, filter, contract, reorder]
  C --> D[work/core_* arrays]
  D --> E[matrix: Dijkstra in waves]
  E --> F[out/: matrix, IDs, report]
  F --> G[verify: symmetry, metric checks, direct reference paths]
```

| Stage | CLI | Main job | Restart behavior |
| --- | --- | --- | --- |
| 1 | `python run.py graph` | Read shapefiles, build weighted road/ferry graph, attach localities | Rebuilds stage-1 files |
| 2 | `python run.py reduce` | Remove irrelevant components, contract graph, reorder CSR | Auto-resumes compatible atomic checkpoints |
| 3 | `python run.py matrix` | Dijkstra from sources, reexpand to towns, save `.npy` and `.csv` | Auto-resumes row/radius checkpoint if it was computed on the same core graph |
| 4 | `python run.py verify` | Check numerical invariants and compare original graph | Repeats the verification |

`python run.py all` runs these stages sequentially. Each stage records what it
was built from, and the later stages stop with an instruction instead of
mixing files from different runs:

| Situation | Detected by | Result |
| --- | --- | --- |
| A stage-1 setting in `config.py` changed after `graph` | `reduce`, `matrix`, `verify` | Stop: rerun from `graph` |
| Any input shapefile or sidecar changed after `graph` (SHA-256, even with the same size and time) | `reduce`, `matrix`, `verify` | Stop: rerun from `graph` |
| `graph` was rerun but `reduce` was not | `matrix`, `verify` | Stop: rerun `reduce` |
| The matrix checkpoint belongs to another core graph | `matrix` | Checkpoint discarded; rows recomputed |
| `out/` was produced from another core graph or other inputs | `verify` | Stop: rerun `matrix` |

Because the later stages re-read the input shapefiles to compare their hashes,
they need the input folder. `graph` records the folder it read in
`work/sources.npz`, and `reduce`, `matrix` and `verify` reuse it when you do
not pass `--data` (or set `NET_DISTANCE_PH_DATA`). If you moved the inputs,
pass `--data` with the new location; the hashes still decide whether the files
are the same. For a new snapshot, a fresh `--work` and `--out` directory is
still the clearest record.

## Command-line reference

```text
python run.py [all|graph|reduce|matrix|verify]
  [--data DIR] [--work DIR] [--out DIR]
  [--workers N] [--batch N]
  [--waves "1,2"] [--wave-scale F] [--no-partial]
  [--restart-reduce]
```

- `--data` finds the three shapefiles recursively under one directory. More
  than one matching basename fails, preventing accidental mixed vintages.
  If the directory holds `current_transport.json` or `current_boundaries.json`
  (written by the builder scripts), the release they select is used instead.
  Later stages default to the directory recorded by `graph`.
- `--work` stores large intermediate graphs and checkpoints.
- `--out` stores deliverable matrices and reports.
- `--workers 4` uses four worker processes; default is configured as
  `N_WORKERS = 0` meaning CPU count minus one.
- `--batch 8` groups eight sources per Dijkstra call.
- `--waves "1,2"` prioritizes the first two Euclidean neighbor rings.
  `--waves ""` disables limited waves but retains the final full run.
- `--wave-scale 1.5` inflates radius estimates from Euclidean geometry.
- `--no-partial` disables output rewriting at each wave boundary.
- `--restart-reduce` discards the compatible stage-2 checkpoint, not
  the stage-1 source artifacts.

Settings also include `SNAP_DECIMALS`, ferry connectors and the ferry
terminal rule, the centroid snap limit, optional endpoint snap offsets, source
ordering, and verification sample sizes. See the commented
`config.py` for the complete set.

## Stage 2: graph reduction

The reducer writes timestamped progress messages to
`work/reduce_progress.log` between steps. After de-duplication and removal of
components without towns, it writes an atomic `work/reduce_checkpoint.npz`, so
an interruption does not repeat those steps. It checks the stage-1 file sizes
and timestamps before resuming. Checkpoints written by the older degree-k
contraction are also accepted.

```bash
python check_reduce_status.py           # add --work DIR if you used --work
python run.py reduce                    # resume, if checkpoint exists
python run.py reduce --restart-reduce   # start this stage from scratch
```

The reduction checkpoint signature is an **operational resume guard** based
on stage-file metadata. In addition, the pipeline now
checks SHA-256 for **every original input shapefile component** against the
manifest embedded in `work/sources.npz`, including after a source file has been
replaced with one of the same size and modification time. The matrix stage
publishes `out/source_manifest.json`. Preserve the upstream source snapshots
and their acquisition provenance for full scientific reproducibility.

## Stage 3: safe partial matrices

Rows are written to a memory-mapped `.npy`. A separate radius array is saved
after matrix data are flushed; after a crash, the checkpoint can safely
recalculate rows rather than wrongly claim completeness. With default settings,
`out/` also receives a labelled partial matrix after each wave; every file in
`out/` is replaced atomically, so an interruption never leaves a truncated
file. Ctrl-C stops the worker processes at once and saves the rows already
finished.

Do not interpret an `inf` entry from an incomplete run as geographic
unreachability. [Output semantics](outputs.md) explains exactly how to tell
these apart.

For the optional, non-distance preflight, run `python run.py connectivity`
after `graph` and before `reduce`. See [connectivity](connectivity.md).

## Installed CLI and controlled stage ranges

Use the unified [`net-distance-ph` CLI](cli.md) for `status`, `doctor`, individual stages, and reproducible stage ranges, such as `net-distance-ph run --until connectivity`. The older `python run.py` interface remains supported.

## Resource use and expected scale

The official Q2 2026 roster lists 1,642 localities, but actual graph size and performance are unmeasured until a new run. Runtime depends on topology, memory, storage, and worker configuration. No inherited speed benchmark should be used as an estimate.

For your first new full-national run, record RAM and graph cardinalities after
stage 1, run stage 2, and run verification before treating the matrix as
research-grade. National inputs are not included in this repository.
