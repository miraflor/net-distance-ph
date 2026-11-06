# Reproducibility, provenance and attribution

## A reproducible run is more than code

To reproduce a numerical matrix, you must preserve **both code and input
vintages**. A local rerun against a newer OSM export or updated administrative
roster can be valid yet numerically different.

Record these inputs for each dataset release:

| Artifact | Minimum metadata |
| --- | --- |
| Road shapefile | Source/export URL, date, filtering rule, CRS, license, SHA-256 of `.shp/.shx/.dbf/.prj` |
| Ferry shapefile | Same plus route-tag filtering and terminal review |
| Locality point shapefile | Administrative code vintage, source, point-generation method and ID count |
| Polygon source (if used) | PSA roster version, geoBoundaries boundary ID/build date and licensing metadata |
| Code | Git commit hash, Python version, NumPy/SciPy/pyproj versions |
| Run | Exact `config.py` parameter values, `--workers`, wave settings, output report |
| Validation | `verification.txt`, sampled physical route audit, checksums of final artifacts |

After making the matrix, capture a local list of input hashes. For example:

```python
from hashlib import sha256
from pathlib import Path
for file in sorted(Path("data").rglob("*")):
    if file.is_file():
        print(sha256(file.read_bytes()).hexdigest(), file)
```

For very large files, consider streaming rather than `read_bytes()` to avoid
unnecessary RAM use. The independent boundary builder writes `boundary_provenance.json` and
`boundary_match_audit.csv` automatically; the polygon-to-centroid converter
adds its own provenance manifest. The matrix pipeline now writes `out/source_manifest.json` with SHA-256 of the
input shapefile components, the relevant graph settings, and core identity.
It checks the inputs again before later stages and refuses stale work files.
Keep a separate full project/software environment snapshot and the original
OSM/administrative source files for long-term reproducibility.

## Source acknowledgments

- **OpenStreetMap contributors:** road and ferry geometry in the historical
  project came from OSM-derived shapefiles, typically via
  [Geofabrik](https://download.geofabrik.de/asia/philippines.html). Review the
  [OSM copyright/ODbL terms](https://www.openstreetmap.org/copyright),
  particularly before redistributing derived databases or published matrices.
- **Philippine Statistics Authority:** the official
  [Philippine Standard Geographic Code](https://psa.gov.ph/classification/psgc/municipalities)
  identifies administrative units and their names/codes.
- **geoBoundaries:** [geoBoundaries](https://www.geoboundaries.org/)
  provides ADM3 boundary geometry in its open offering; cite the exact dataset
  metadata, original contributing boundary sources and relevant license.

The GitHub repository **includes the official PSA PSGC Q2 2026 XLSX and derived JSON**
under `reference/psgc/`, with SHA-256 source attribution. It intentionally omits other
national raw GIS files, intermediate
networks and final distance matrices. **Publishing computed distance matrices
may itself raise database-license and attribution questions**; do not assume
that distributing code automatically gives permission to distribute data.

## GitHub Pages deployment

1. Create a new repository named **`net-distance-ph`**, then push this
   source tree. Do not upload the old `work/`, `out/` or private input folder.
2. The site and repository URLs in `mkdocs.yml` target `miraflor/net-distance-ph`. Update them only if you publish under a different owner or repository.
3. In repository **Settings → Pages → Build and deployment**, choose
   **GitHub Actions** as the source.
4. Push to `main`, or trigger the **GitHub Pages** workflow manually under
   Actions. The workflow builds MkDocs Material and publishes `site/`.
5. Review the rendered site, its links and the release's data provenance.

GitHub shows the published Pages URL after successful deployment; the site has
not been published by this source package.

For local preview:

```bash
python -m pip install -e ".[docs]"
python -m mkdocs serve
```

## Code license and upstream data licenses

The project's source-code files are provided under the **MIT License** (`LICENSE` at
repository root). Review and change that choice before publishing if it does
not match your intended grant of rights. The MIT code license **does not
relicense** underlying OSM, PSA, or geoBoundaries source data. Preserve dataset-
specific attribution, redistribution conditions, and license metadata;
particularly review OSM's ODbL when considering derived network exports.

## Publication review

- [ ] Source files and dataset provenance are pinned and cited.
- [ ] No absolute private system path or proprietary input is committed.
- [ ] ID roster vintage and centroid method are explicit.
- [ ] Code tests and `verify` both pass for the target national dataset.
- [ ] Disconnected islands and centroid snaps are audited.
- [ ] Matrices are not distributed before the applicable data license review.
- [ ] `mkdocs.yml` is pointed to the actual repository.
- [ ] MIT software license is approved by the publisher and dataset attribution is complete.
