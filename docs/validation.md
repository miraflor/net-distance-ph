# Validation, tests and limitations

## Automated tests

Run locally or in pull-request CI:

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

The tests cover:

- Geographic distance formulas and coordinate behavior.
- Shapefile record parsing (including Null-shape records), multipart shapes
  and text identifier preservation; rejection of numeric, empty or duplicate IDs.
- Ferry **endpoint** connection behavior and the optional terminals-only
  snapping fallback.
- Graph de-duplication, contraction and source-node mapping.
- Wave radii, incomplete checkpoints, symmetry and resume invariants; a
  checkpoint from a different core graph is never resumed; a failure stops
  the worker pool without waiting for queued work.
- A complete synthetic-archipelago execution of `run.py all` compared against
  unreduced Dijkstra (no national data required), the triangle inequality on
  every triple, CSV round-trips, a re-run after the inputs change, and refusal
  to combine stage files from different runs.
- Optional polygon-to-centroid preparation and the boundary builder
  (matching, overrides, provenance integrity) if GeoPandas is installed.
- Byte-level input hashes (a changed sidecar with unchanged size and time is
  detected), the documented stage-by-stage commands without `--data`, and a
  preflight check that reports every missing file.
- The OSM transport extractor: road classes, the optional motorway classes,
  ferry-relation member rules (approach roads stay roads; platforms and piers
  are excluded), versioned publication and failure behaviour; and, when
  pyosmium is installed, the real PBF path against the XML path and a clear
  error for a way with a missing node.

GitHub Actions runs tests on Linux and Windows using Python 3.11 and 3.13.
That matrix installs only `.[test]`, so the GIS and pyosmium tests skip
there. A separate Linux job installs `.[test,geo,transport]` and runs the
whole suite, and a third job builds the documentation in strict mode on every
pull request.

## Verification against the full graph

```bash
python run.py verify
```

The stage first confirms that `work/` and `out/` belong to the same run
(same stage-1 settings, same stage-1 files, same core graph) and stops if
not. It then checks, writing `PASS`/`FAIL` per line to `verification.txt` and
exiting non-zero on any failure:

- the asymmetry measured *before* the output was made symmetric
  (`max_asymmetry_relative` in `matrix_report.json`) is at most
  `VERIFY_MAX_ASYMMETRY_RELATIVE`;
- the output is exactly symmetric, non-negative, with a zero diagonal (true by
  construction; kept as a sanity check);
- the **published** NumPy matrix and row-validity radii match an independently
  reconstructed result from the reduced-core checkpoint; this catches symmetric
  and triangle-compliant corruption that structural tests cannot detect;
- the published `INCLUDE_SNAP_OFFSET` setting agrees with the configuration;
- 200,000 random triples satisfy the triangle inequality within
  `VERIFY_TOL_KM`;
- critically, a configurable sample of completed source rows recalculated
  against the **unreduced** graph agrees within `VERIFY_TOL_KM`, with no
  reachability mismatch. Use `VERIFY_N_SOURCES` to choose the sample size.

In the archived original run, 40 reference-source rows had no reachability
mismatch and maximum difference about `2.14e-11 km` before the final
symmetrization; this is a historical result, not a claim about new data.

!!! warning "What verification does not prove"
    Agreement between the reduced and unreduced graphs proves the optimization
    preserves the *constructed graph's* paths. It does **not** prove that the
    OSM-derived graph correctly represents actual highways, ferry operations,
    legal access, traffic constraints, or settlement locations.

## Major analytical limitations

| Issue | Implication |
| --- | --- |
| Ferry routes incompletely mapped | Real islands may appear unreachable |
| Ferries and roads treated as undirected | Direction, service schedules, restrictions not modeled |
| Limited road classes in the original layer | Local street accessibility may be omitted |
| Automated terminal snap up to 5 km | Can create unsupported shore connections; review links |
| Centroids snapped up to 25 km | Long and potentially implausible connectors can dominate small-area distances; a connector can cross water to another island's road |
| Fallback snap to any ferry node (default) | A roadless island can attach mid-route to a ferry that does not stop there; see `SNAP_FALLBACK_TERMINALS_ONLY` |
| Geometric polygon centroids | Not population centers or municipal halls; may fall outside polygons |
| Rounding/noding based on coordinates | At-grade crossings can be omitted; distinct elevated levels could merge |
| Optional ferry cost penalty per segment | Not ferry waiting time or one penalty per vessel crossing |
| Snapshot-specific roster and codes | New PSGC releases require explicit crosswalk and new outputs |
| No verified source hashes in old archive | Historical result cannot be exactly re-created from the code alone |

### Triangle inequality and snap offsets

With point-to-network snap offsets, the computed off-diagonal value is
`offset_i + graph_distance(i,j) + offset_j`, with the diagonal set to zero.
This is still a metric: a two-leg trip a→b→c pays `offset_b` twice, which can
only make it longer, so `D[a,c] <= D[a,b] + D[b,c]` holds for every triple
(up to floating-point rounding). The verifier samples triples and fails on a
violation; the synthetic end-to-end test checks every triple. Earlier
versions of this page said the opposite; that was an error.
`INCLUDE_SNAP_OFFSET = False` is still the right choice when you want pure
node-to-node network distance, but not because of the triangle inequality.

### Spatial econometric use

If deriving contiguity or distance-based spatial weights, define the
threshold, how unreachable pairs are treated, and whether to use full network
kilometres, snap-only values, or a transformed proximity function. Never
replace `inf` with zero without a documented convention: it would turn
unreachability into artificial proximity.

## Recommended audit before publication

1. Freeze and hash all shapefile components and the PSGC roster.
2. Inspect `snap_tier`, large offset outliers and disconnected islands.
3. Manually sample computed routes against a trustworthy independent GIS graph.
4. Run the complete CLI regression suite and final `verify` stage.
5. Record parameter values, Python package versions and hardware.
6. State whether the result is network distance or generalized equivalent cost.
