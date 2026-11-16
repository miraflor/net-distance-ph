# Road-class profiles and nationwide default

**Default:** `comprehensive` (14 OSM `highway=*` classes). The `major` four-class selection remains available for comparisons; because access filtering is now applied, a newly extracted `major` release is not necessarily byte-identical to the previous four-class output. The old immutable transport release is retained for exact replication. Neither is a licensed navigation system: this is an **undirected geodesic-distance graph** with no direction-of-travel, turn restrictions, toll preferences, schedules or travel times.

| Profile | Selected highway values |
| --- | --- |
| `major` | `trunk`, `primary`, `secondary`, `tertiary` |
| `comprehensive` (default) | `trunk`, `primary`, `secondary`, `tertiary`, `unclassified`, `residential`, `motorway`, `service`, `living_street`, `trunk_link`, `primary_link`, `secondary_link`, `tertiary_link`, `motorway_link` |

Motorways are necessary for major Philippine expressways such as NLEX and SLEX; `motorway_link` and other selected `_link` roads represent access/exit ramps and connectors. The additional `unclassified` and `residential` ways recovered a local OSM path across all nine short component gaps examined on the 2026-11-16 source snapshot. That test did **not** prove national connectivity or actual legally traversable routes. `service` includes ordinary commercial/industrial access roads but also private roads and parking aisles. Explicitly tagged `access`/`vehicle`/`motor_vehicle`/`motorcar` restrictions of `no`, `private`, `agricultural`, `forestry` or `delivery` are excluded, with the more specific tag taking precedence. Conditional access, `destination`, `customers`, service-subtype exclusions and turn direction are **not** fully handled; these require a later routing-accuracy audit.

## Run using your existing cached national PBF

```powershell
net-distance-ph run --until connectivity --verbose
```

The first `prepare` phase checks the selected transport release's exact `road_filter` against the requested profile. On the first profile change, it archives the old graph, sources, and connectivity audit to `workspace/baselines/transport-<old-release-id>/`, then re-extracts a *new immutable transport release* from the already cached `data/sources/philippines-latest.osm.pbf`. It requires the PBF SHA-256 to match the existing release; a changed or absent PBF is an error, not a download request. Source preparation does not silently modify the earlier versioned transport release. After preparation, the `graph` phase recomputes its graph; a subsequent `connectivity` phase compares components. Future prepares can reuse the selected comprehensive release.

Manually published class combinations outside these two named profiles are not automatically retained by a later `prepare` run; use `graph --no-auto-prepare` deliberately when evaluating a custom transport release. The normal CLI uses the selected named profile and re-extracts when its recorded class set differs.

The reference profile can be selected explicitly for a controlled rerun:

```powershell
net-distance-ph run --until connectivity --road-profile major --verbose
```

This replaces the *selected release pointer* after archiving the previous graph, but does not delete the comprehensive transport release. Every graph rebuild carries input fingerprints: any previously reduced core or matrix is stale until rebuilt. Do not mix outputs from different selected releases.

## Later class-based cost calibration

Each extracted road polyline retains `OSM_ID` and `OSM_CLASS` in its shapefile attribute table, and the release manifest saves its exact class filter. The existing graph builder uses road class only for inclusion and treats every road edge as a geodesic length; it does **not** yet preserve `OSM_CLASS` for individual graph edges. To calibrate distances or travel costs by road type, a later schema change must propagate class IDs into graph edges, decide how to handle duplicates/overlaps, use appropriate directional restrictions, and rerun reduction and matrix computations. This is intentionally deferred.

**Performance:** More selected roads means more polyline vertices and graph nodes/edges. The national factor cannot be inferred from nine gap locations or raw OSM-way counts. Measure extracted way counts and `graph` stage size/time, then compare the new connectivity report to the 43-component historical baseline. The 1,642 by 1,642 matrix dimensions do not change; graph construction, reduction and shortest paths may become substantially more expensive.
