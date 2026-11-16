# PSGC-aligned municipal boundaries (optional source)

The default `prepare` stage uses older geoBoundaries ADM3 polygons and can fail to reconcile the PSA Q2 2026 municipality/city roster. This is expected when geographic vintages differ; **do not waive incomplete matches**.

An alternative source is [Philippines PSGC Shapefiles v1.0.1](https://github.com/faeldon/philippines-psgc-shapefiles/releases/tag/v1.0.1), whose ADM3 release is aligned to PSGC Q3 2026. Its published ADM3 attribute CSV matches all 1,642 codes, names, and city/municipality types in this repository's pinned PSGC Q2 2026 roster (verified in November 2026). Crucially, **Manila is one city** (`1380600000`); the 14 sub-municipal districts are *not separate routing origins*. Eight BARMM Special Geographic Area municipalities are included.

The upstream polygon geometry derives from older OCHA / PSA / NAMRIA COD-AB data, with subsequent administrative transformations. It is not a legal survey of all 2026 boundaries. Review the [upstream methodology and licensing](https://github.com/faeldon/philippines-psgc-shapefiles) before use or redistribution.

## Download and validate

Install the package with its geography dependencies in your research environment.

```powershell
conda activate thesis
cd "$HOME\Documents\GitHub\net-distance-ph"
python -m pip install -e ".[test,geo,transport]"

# Download the pinned 160 MB ZIP automatically, verify SHA-256,
# read the geometries, and validate the 1,642 official records.
# No boundary release is published.
net-distance-ph boundaries --download
```

Alternatively, manually download `PH_Adm3_MuniCities.shp.zip` from the [v1.0.1 release](https://github.com/faeldon/philippines-psgc-shapefiles/releases/tag/v1.0.1):

```powershell
net-distance-ph boundaries --source "$HOME\Downloads\PH_Adm3_MuniCities.shp.zip"
```

`--download` caches only the pinned v1.0.1 ZIP under `workspace/data/sources/psgc-aligned/` and verifies its SHA-256 (`911d025e6da37e55d9cf04c872b1acaf9a6302487a3f0c0fb68d683d9bc982b3`). A corrupt existing cache is rejected, not silently replaced.

## Publish the boundary release, then build the national graph

After validation passes and you've reviewed the geometry's uncertainty and provenance:

```powershell
net-distance-ph boundaries --download --publish
net-distance-ph run --until connectivity --verbose
```

Publishing creates a normalized, provenance-tracked review GeoPackage in `workspace/data/boundary_review/`, then invokes the **existing**, atomic `scripts.build_boundaries` publisher with `--adm3` and `--skip-adm2`. No geoBoundaries ADM3 polygons are substituted or joined. The builder must still match every official PSGC code before selecting a complete release.

If you intentionally need to replace an already-selected boundary release, use `--replace` along with `--publish`. This retains the previous release files, but explicitly changes the selected release pointer. Do not use `--replace` just to bypass errors.

`net-distance-ph run --until connectivity` then reuses the selected boundary release, downloads/builds the independent road/ferry transport network if needed, constructs the graph and audits locality connectivity. The matrix stage is not run. A valid boundary polygon is **not** proof of accessible roads or current ferry service.

## Validations and explicit limitations

The adapter requires the exact SHA-256 of the pinned upstream ZIP; 1,642 unique PSGC IDs, matching names and locality types; declared projected CRS; nonempty valid Polygon/MultiPolygon geometries; and plausible Philippine geographic extent. It checks a one-to-one Manila city polygon and rejects duplicate/missing municipalities. The resulting GeoPackage is checked by a read-back and linked to both the source ZIP checksum and the pinned official roster checksum.

Those tests establish **identity and geometry file integrity**, not contemporary legal boundary correctness. The report and builder provenance should be retained for replication. No national run, snap coverage, ferry accessibility, or shortest-path results are claimed by this patch.
