<!-- v55-methods-documentation -->
# Execution status and evidence

There are two distinct forms of evidence: **(a) tests and local execution reports** and **(b) a portable, independently reproducible published national dataset**. They must not be conflated.

## Maintainer-local evidence (reported 18 November 2026)

The maintainer has run the **population-weighted** national workflow and its geography/topology diagnostics locally. Reported observations include 1,642 official cities/municipalities, 112,723,109 modeled residents in the studied input population, preserved connected-component lineage through graph reduction for 10,505 population representatives, and six polygon/network overlay checks. The v54.1 Windows test suite reported **369 passed, one timing-sensitive skip, and nonfatal Shapely deprecation warnings**. These are *maintainer-supplied execution records*, not measurements reproduced by a separate investigator using a repository-published national input snapshot.

The main outputs, cached OSM inputs, geometries, complete provenance package, and numeric national matrices are **not committed to this source repository**. This documentation therefore does not publish a certified nationwide distance dataset, operational ferry-service assessment, or verified time/cost travel model.

## Method status

| Workflow | Code status | Published national data status |
| --- | --- | --- |
| Population-weighted town OD (primary tier of default three-level export) | Implemented; component-aware balanced k-medians; local tests and reported national run | National matrix not included in repository |
| Province-equivalent / region OD | Implemented as **default aggregations** of town OD totals; population conservation tests | National upper-tier matrices not included |
| Legacy locality centroid OD | Separate implemented graph/reduction/wave-routing pipeline | National baseline matrix not included |
| Topology/origin consistency audits | Read-only checks implemented and locally run by maintainer | Audit findings are conditional on the local source snapshots |

## Before publishing a numerical release

Pin SHA-256 values for the PSGC/population and origin geometry data, selected OSM extract and filter, graph, code commit and selection settings; preserve the row identifiers and all population/coverage/matrix arrays. Include both `.npy` and `.csv` matrices and complete manifests; report disconnected/unmodeled origin populations separately. Verify selected site-to-site paths on the unreduced graph, and state that *model connectivity is not operational ferry availability*. See [reproducibility](reproducibility.md), [validation](validation.md), and [population-weighted methodology](population_weighted.md).
