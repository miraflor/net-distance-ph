# Execution status

The repository contains implementation code, official reference data and synthetic regression tests. None of those establish a completed nationwide network computation.

## Known execution evidence

The maintainer has not confirmed a completed nationwide `net-distance-ph` computation, and explicitly states that they have not yet run the project for this purpose. Inherited or previously described numerical results and synthetic fixtures do **not** establish results from a user-run national graph.

There is no demonstrated nationwide graph connectivity result, complete distance matrix, or audited set of actual route paths.

## First-run acceptance tests

1. Verify exact coverage of the 1,642 official Q2 2026 PSGC cities and municipalities.
2. Ensure every locality snaps to a plausible network node; flag unusual connectors and ferry-only snaps.
3. Check connected components of the combined road-and-ferry graph; for modeled all-town reachability, every locality must share one component.
4. Independently review questionable ferry termini, operating services, topology and legal access.
5. Only then compute the full distance matrix and retain input hashes, code revision, settings and numerical verification.

The initial national result is **pending**, not known.
