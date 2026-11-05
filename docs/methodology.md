# Methodology

This is a **graph shortest-path model** with geometric distance weights. Its
primary choices are explicit so that the meaning of the matrix can be assessed
independently of its computational optimizations.

## 1. Construct an undirected spatial graph

Every road or ferry polyline is split into consecutive coordinate pairs. Each
pair contributes a graph edge. Coincident vertices, after rounding coordinates
to `SNAP_DECIMALS = 6` decimal places, are treated as the same node. Multipart
polylines are not connected across their part boundaries.

For a segment with endpoints \(a,b\) the geometric weight is

\[
 w_{ab}=d_{\mathrm{WGS84}}(a,b)\quad\text{kilometres}.
\]

When `pyproj` is available, `Geod` computes ellipsoidal geodesic lengths.
Otherwise the bundled Lambert approximation is used. The graph is **not
turn-restricted** and does not model one-way roads. Contraction and Dijkstra
operate on its non-negative **undirected** edge weights.

Vertex noding alone is not a full planar crossing or topological validation:
non-coincident line crossings do not become junctions, and exactly coincident
points at different elevations could be merged. Review overpasses and
interchanges when topology matters.

## 2. Connect ferry endpoints to roads

Ferry geometry that shares an existing graph node with a road already connects.
Otherwise, a **ferry-polyline endpoint** can be linked to the nearest road node
up to `FERRY_CONNECT_KM` (default **5 km**); its connector costs its own
geodesic length. Intermediate ferry shape vertices are **not** automatically
attached merely because they pass near the shore.

With `FERRY_COST_MULTIPLIER = 1` and `FERRY_FIXED_PENALTY_KM = 0`, ferry
travel contributes its geometric length. For a ferry edge \(e\), the optional
generalized-cost rule is

\[
 w_e^\ast = m\,w_e+p.
\]

**Important:** `p` is applied per **ferry polyline segment/graph edge**, not
per actual scheduled ferry departure or complete crossing; do not interpret
it as a calibrated waiting time. If adjusted, the result is an equivalent-
kilometre **cost matrix**, not literal route distance.

## 3. Attach each town's reference point

Each town origin is attached to a nearby graph node. With
`PREFER_ROAD_SNAP = True`, the pipeline searches road nodes first, then any
road/ferry node, and rejects a point further than
`MAX_CENTROID_SNAP_KM = 25` km. Nearest-node queries use a KD-tree on
Earth-centered Cartesian coordinates and the actual connector length is
recorded geodesically. The tree ranks by three-dimensional chord distance,
which is a close approximation to, but not a proof of, exact geodesic-nearest
ranking on an ellipsoid.

The fallback ("any node", `snap_tier = 1`) can find only ferry nodes when
road nodes were tried first, and the nearest ferry node is not always a
terminal: it can be an intermediate shape point of a route that passes an
island without stopping there. `SNAP_FALLBACK_TERMINALS_ONLY = True` excludes
such shape points, applying to towns the rule that already governs gangway
edges. The default (`False`) keeps the historical behaviour.

If \(s_i\) is the snapped node for locality \(i\), and \(o_i\) its
geometric connector distance, the output distance is

\[
 D_{ij}=o_i+\underbrace{\min_{P:s_i\leadsto s_j}
   \sum_{e\in P}w_e}_{\text{shortest network path}}+o_j,
 \qquad i\ne j,
\]

when both points are reachable. The diagonal is defined as \(D_{ii}=0\).
With `INCLUDE_SNAP_OFFSET = False`, the connector costs are excluded.

!!! note "Triangle inequality and connector offsets"
    The network-node shortest path satisfies the triangle inequality, and
    adding the connectors keeps it: for distinct \(a,b,c\),
    \(D_{ab}+D_{bc} = o_a + d(s_a,s_b) + 2o_b + d(s_b,s_c) + o_c \ge
    o_a + d(s_a,s_c) + o_c = D_{ac}\). The intermediate town's connector is
    paid twice, which only adds slack. The verification stage therefore
    treats any sampled violation beyond floating-point rounding as a failure.
    (Earlier documentation stated the opposite; that statement was wrong.)

## 4. Reduce the graph without altering shortest paths

The original graph can contain millions of shape vertices with degree two.
The contraction stage removes selected non-source nodes, retaining direct
shortcuts between their neighbors. For a removed node \(v\), a shortcut
from \(u\) to \(w\) has weight \(w_{uv}+w_{vw}\). Because it corresponds to
an actual path, contraction preserves shortest paths for surviving nodes.

Contraction is constrained by `MAX_CONTRACT_DEGREE` and `MAX_EDGE_GROWTH`,
so it does not indiscriminately explode edge count. Equal parallel edges are
deduplicated by their **minimum** weight before CSR construction; ordinary
sparse-matrix construction can otherwise *sum* duplicate entries incorrectly.
After contraction, reverse Cuthill–McKee reorders node IDs for locality of
memory access, without changing distances.

## 5. Multi-source Dijkstra in waves

Each locality source generates an entire shortest-path row. The sources can
run in parallel; workers memory-map the same reduced CSR graph. An optional
k-nearest-neighbor ordering makes early partial results geographically useful.

Two radius-limited Dijkstra waves prioritize nearby pairs (orders 1 and 2 by
default in a Euclidean kNN graph); a final unrestricted wave always computes
the entire reachable row. This scheduling changes **when results arrive, not
the final result**.

For a row with validity radius \(R_i\):

- Any finite value already saved is exact and final.
- An infinite entry means its network distance is **beyond \(R_i\)**, or it
  may be unreachable, until the unrestricted wave completes.
- When \(R_i=\infty\), infinity means unreachable under this input graph.

The search-radius estimate does not guarantee all Euclidean neighbors have
been found: a nearby town may require a long road detour.

## 6. Reexpand, symmetrize and verify

The finished core-node matrix is expanded to all input localities, including
multiple towns attached to a common node. Symmetry is enforced after checking
and reporting floating-point residuals; the verification stage fails if that
residual exceeds `VERIFY_MAX_ASYMMETRY_RELATIVE`. Verification also compares
selected completed rows against **fresh Dijkstra calculations on the unreduced
graph**, checking that contraction, reordering, and wave scheduling did not
change paths.

For original implementation details and local benchmarks, see
[historical engineering notes](engineering-notes.md). Those measurements are
not promises for a new dataset or computer.
