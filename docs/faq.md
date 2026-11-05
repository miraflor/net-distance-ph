# Frequently asked questions

### Does this create real driving-time estimates?

No. It measures geometric path length in kilometres on a road/ferry graph. It
has no speed limits, traffic data, route schedules, one-way/turn restrictions,
or transfer delays. Optional ferry multipliers create generalized equivalent-
kilometre **costs**, not time.

### Is the matrix symmetric?

The model uses undirected edges, so yes (after the final numerical symmetry
check and postprocessing). Actual travel can be directional; that is not
modeled here.

### Is it a metric (does the triangle inequality hold)?

Yes, with or without the connector offsets; `verify` checks it on a sample
of triples. See [methodology](methodology.md).

### Why are some pairs infinite even in the completed matrix?

Some localities are attached to disconnected network components or not snapped
at all. A ferry may be missing from the chosen snapshot. It means
**unreachable in this graph**, not necessarily in real life.

### What if a partial matrix contains infinity?

Use `row_validity_radius_km.npy`. A finite radius means the search for that
row is incomplete; an infinite radius means its unrestricted Dijkstra run
finished. Check [output interpretation](outputs.md).

### Why not use straight-line centroid distances instead?

Euclidean or geodesic distances ignore roads and ferry links. The network
matrix captures some detours and discontinuities, but introduces its own
model and data-quality assumptions.

### Are towns represented by municipal halls?

Not by default. The point input represents geographic reference points, and
the optional boundary converter uses geometric centroid or representative
point. These are not municipal hall coordinates or population centroids.

### Can I generate municipal boundaries without any other project?

Yes. Use `python scripts/build_boundaries.py --output data`. It sources the
PSA PSGC roster and geoBoundaries polygons independently, writes matching
audits, and generates a locality GeoPackage and point shapefile **only when
coverage is verified**. The two public sources have different vintages, so
reviewed overrides may be needed. Road/ferry lines are separately constructed using the
[independent transport builder](transport.md), which can download an OSM
Philippines PBF and extract both way-tagged and relation-member ferry ways.
The boundary builder and transport builder each select a versioned output
release; the matrix runner resolves both selections automatically.
Read the [boundary guide](boundaries.md).

### Can I build the national transport network myself?

Yes. Run `python scripts/build_transport.py --download --output data`
after installing the `[transport]` extra. A cached OpenStreetMap extract is
used to generate WGS84 road and ferry line shapefiles. The result has a
content-hashed provenance manifest and is not a statement about ferry
operating status, road restrictions, or full national network correctness.
See [transport inputs](transport.md).

### Can I reuse the old matrix when I replace the boundaries?

Not if the new reference points change coordinates or IDs. Generate a new
matrix and retain both vintages, with explicit metadata and a validated code
crosswalk when comparisons are necessary.

### Why is `work/` so large and ignored by Git?

It contains arrays for the national graph, reduced CSR, resume checkpoints
and temporary matrices. Those can be regenerated from original inputs; they
are excluded from the source repository to prevent accidental distribution of
large or licensed datasets.

### Why is there a warning that the original PSGC IDs were different?

The archived outputs use `PH...` identifiers, not consistently the current
PSA ten-digit PSGC representation. Identifier equality is a data-matching
claim, not a matter of formatting. Require an explicit concordance.

### How can I reproduce the original 1,642-town matrix exactly?

You need the original shapefiles, exact point coordinates and IDs, the
corresponding config parameters and dependency versions. The archive of the
original run held outputs and intermediate graph files, **not the three
source shapefiles**. The repository alone therefore cannot reconstruct the
old source snapshot.
