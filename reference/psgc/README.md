# PSA PSGC — Q2 2026 pinned source

This directory contains the original **Philippine Statistics Authority (PSA)** PSGC
publication workbook (dated **30 June 2026**) and machine-readable derivatives.
The original XLSX was supplied for this repository on 6 November 2026 after
an attempted direct download from the official PSA URL returned HTTP 403.

| File | Contents |
| --- | --- |
| `PSGC-2Q-2026-Publication-Datafile.xlsx` | Unmodified official workbook (all sheets, formulas, annotations and metadata) |
| `psgc_q2_2026_all.jsonl.gz` | Compressed JSON Lines representing every populated geographic record in the workbook's `PSGC` sheet (43,768 rows, including two unclassified group records) |
| `psgc_q2_2026_localities.json` | Validated, normalized 1,642-city/municipality roster used by the boundary builder |

**Original XLSX SHA-256:** `31892bc2bdde3ea0682562d9412b5bab4d45a0be5e5a5b4f6c9d7714b94bca5d`

**Publication source:** <https://psa.gov.ph/system/files/scd/PSGC-2Q-2026-Publication-Datafile.xlsx>

**Attribution:** Philippine Statistics Authority, Philippine Standard Geographic Code,
Q2 2026 publication. The workbook itself lists access constraints as `None`
and requests acknowledgement of the PSA as source. PSA data and materials are
not relicensed under this repository's MIT code license.

The PSGC sheet has 18 region records, 82 province records, 149 city records,
1,493 municipality records, 14 submunicipal district records and 42,010 barangay
records. Two additional rows (`0990100000`, `1999900000`) lack a geographic
level in the spreadsheet; the full JSONL preserves them **without assigning a
made-up level**. They are not silently included in the city/municipality roster.

Localities use ten-digit PSGC **strings**. Province names are filled only when a
province record with the corresponding first five digits exists; otherwise the
field stays blank rather than inventing a provincial assignment. The JSONL
retains fields as strings, with `excel_row` for source traceability. Blank
worksheet cells are absent from individual JSON records.

Recreate the two derived datasets and verify they agree byte-for-byte with the
committed versions using:

```bash
python scripts/convert_psgc.py --check
```

To regenerate deliberately:

```bash
python scripts/convert_psgc.py
```

`run.py prepare` and `scripts/build_boundaries.py` use the bundled locality JSON
by default and therefore do not require a live PSA workbook request. Acquiring
geoBoundaries geometry and OSM road/ferry data is still separate and requires
network access on the first run. An unsuccessful locality-to-geometry
reconciliation still stops and writes an audit.
