# Administrative boundary levels and automatic preparation

The standard pipeline is now self-contained:

```powershell
net-distance-ph run --until connectivity --verbose
```

It automatically downloads the **SHA-256-pinned** PSGC-aligned town/municipal-city polygon archive, validates all 1,642 official Q2 2026 identities and the geometries, publishes a versioned boundary release, prepares the road/ferry network, builds the graph, and audits modeled reachability. If any step fails the run stops before subsequent stages. A manual `boundaries --publish` step is no longer required. Existing selected boundary releases are reused; nothing is overwritten implicitly.

## Geographic unit selection

`--boundary-level` accepts the following values:

| Level | Selection | Official Q2 2026 count | Upstream polygon archive |
| --- | --- | ---: | --- |
| `town` (**default**) | All cities and municipalities, including one City of Manila | 1,642 | ADM3 |
| `city` | All cities, including HUC, independent component and component cities | 149 | ADM3 |
| `municipality` | Municipalities only | 1,493 | ADM3 |
| `huc` | Official highly urbanized cities according to the pinned PSA workbook `city_class` | 33 | ADM3 |
| `province` | Official provinces only, excluding NCR districts and BARMM interim province | 82 | ADM2 |
| `region` | Official regions | 18 | ADM1 |
| `barangay` | Official barangays, excluding Manila submunicipality aggregate polygons | 42,010 | ADM4 **complete** |

```powershell
net-distance-ph run --until connectivity --boundary-level city --verbose
net-distance-ph run --until connectivity --boundary-level huc --verbose
net-distance-ph run --until connectivity --boundary-level province --verbose
net-distance-ph run --until connectivity --boundary-level barangay --verbose
net-distance-ph boundaries --download --boundary-level region  # validation only
```

For non-town levels, the CLI uses `~/Documents/net-distance-ph/levels/<level>/` (or the equivalent under `--workspace`). Each has separate `data/`, `work/`, `output/`, and `logs/`; running different levels cannot silently reuse a matrix from another. Do not override `--data`, `--work` or `--out` for non-town modes. By default `town` retains the existing workspace layout. Stage-1 manifest checks and the selected level's official roster are required for connectivity audits.

## Interpretations and limitations

* These are **geographical routing origin selections**, not new algorithms or new transport networks. The road/ferry graph is the same class of modeled network. The origin for each polygon is its equal-area **geometric centroid**; it is not necessarily a town hall, barangay center, inhabited location or accessible road. Snap radius is unchanged (25 km by current config), so large regions/provinces may be unsnappable or snapped offshore.
* Source is the checksum-pinned **v1.0.1, PSGC Q3 2026** third-party reconstruction. Identity validation is against the checked-in original **PSA Q2 2026 workbook**, including the official city/HUC classification. The one Q3 barangay label correction (if present) is reported explicitly while retaining the official Q2 code and name. All other name/code disagreements fail.
* **Barangay complete** contains four approximated 2024 barangay boundaries (and revised mother polygons). These are *indicative*, not surveyed. The default known-only upstream barangay archive omits those geometries. The complete archive is used solely to support a 42,010-origin evaluation; the provenance records the approximation limitation.
* A full barangay matrix has **42,010² = 1,764,840,100 ordered cells** before auxiliary arrays, requiring substantial disk/RAM and time. `run --until connectivity` does **not** compute that matrix. Component diagnostics use a bounded 100-pair summary rather than enumerating O(N²) disconnected pairs.
* Region/province/HUC are not interchangeable economic or political units. NCR's districts and the BARMM interim province are not counted as provinces. This mode selects PSA `Prov` records only.
* **Connected in the modeled undirected graph** does not establish scheduled ferry services, physical travel feasibility, or current surveyed administrative boundaries.
* A source checksum matches a pinned archive, not an independent guarantee of geometric accuracy. Review the third-party [methodology and license](https://github.com/faeldon/philippines-psgc-shapefiles) before using outputs for publication.

The `boundaries` command remains available for validation only (`--download`) or explicit publication (`--publish`). The primary `prepare` command now handles this automatically when there is no valid selected boundary release.
