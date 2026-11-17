# Role-based geographic source filenames

Names describe **what the geometry contains**, not the particular OSM road filter,
region, or boundary level. Starting with newly published releases:

| Selected source role (JSON key) | Canonical filename | Previous filename (still accepted) |
| --- | --- | --- |
| `roads` | `roads.shp` | `primsectertrunk_road.shp` |
| `ferries` | `ferry_routes.shp` | `ferry_route_shape.shp` |
| `centroids` | `locality_origins.shp` | `muncity_centroids.shp` |

The JSON key `centroids` stays stable for backward compatibility. Its canonical
filename is `locality_origins.shp` because the points may be centroids or other
representative points. The road class filter is **not** encoded in a filename:
see `transport_provenance.json` (`road_profile`, `road_filter`) and the road
shapefile's `OSM_CLASS` attribute.

The source selection pointers (`data/current_transport.json` and
`data/current_boundaries.json`) identify the actual paths. They are the source
of truth. `net-distance-ph` accepts both historical and canonical basenames
**only in their respective source roles**; arbitrary pointer paths, missing
sidecars, ambiguous standalone inputs and traversal outside the workspace are
rejected. Old and new names mixed in one manually supplied data root without
pointers are ambiguous and refused.

## Existing national release: no migration necessary

Your already selected comprehensive transport release (for example,
`a91adae5d4254087`) and boundary release stay **immutable**. The new naming
code reads their original filenames via the pointers. Installing this change
**does not re-extract OSM, duplicate release files, rewrite the selected JSON,
change fingerprints, or invalidate the stage-1 graph.**

New transport releases created from OSM will be published with `roads.shp` and
`ferry_routes.shp`. New boundary releases will use `locality_origins.shp`.
Historical source shapefiles and point DBF data remain valid.

Avoid renaming selected files manually. Even a byte-identical rename changes
the recorded path in the graph's source fingerprint; it would require a
controlled new release publication and new graph build. For the current run,
complete connectivity review and matrix verification against the already
selected release first.

## End-to-end checklist after a connectivity audit

```powershell
# Read the current connectivity report first: a failed gate is an audit,
# not a graph-build failure.
Get-Content "$HOME\Documents\net-distance-ph\output\connectivity_audit\connectivity_report.json"

# If remaining disconnections are accepted as valid modeling limitations:
net-distance-ph run --from reduce --workers 2 --verbose

# Inspect final verification.
Get-Content "$HOME\Documents\net-distance-ph\output\verification.txt"
```

The graph model remains undirected and does not enforce one-way roads, turn
restrictions, timetables or all access permissions. The matrix must not be
presented as legally verified driving travel times.
