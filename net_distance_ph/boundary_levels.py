"""PSGC-aligned administrative-level boundaries with an explicit source lineage.

This module is called only for a non-default level. The default ``town`` level
continues to use the existing, fully pinned 1,642-town boundary publisher.
Other levels are isolated by the unified CLI into separate workspaces.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

from . import boundaries as upstream

LEVELS = ("town", "barangay", "city", "municipality", "huc", "province", "region")
# Release v1.0.1 asset hashes, retrieved from the release's SHA256SUMS listing.
ASSETS = {
    "town": ("PH_Adm3_MuniCities.shp.zip", upstream.ARCHIVE_SHA256),
    "city": ("PH_Adm3_MuniCities.shp.zip", upstream.ARCHIVE_SHA256),
    "municipality": ("PH_Adm3_MuniCities.shp.zip", upstream.ARCHIVE_SHA256),
    "huc": ("PH_Adm3_MuniCities.shp.zip", upstream.ARCHIVE_SHA256),
    "province": ("PH_Adm2_ProvDists.shp.zip", "eb1732c26b830201e121e89d5526f0dcd68aad33d2bc3e2d4f22f389c18385ef"),
    "region": ("PH_Adm1_Regions.shp.zip", "e2e8a8e747f9a71fa20c023810a6668a77918e12d1513f7d76ef1cc0d8556ec6"),
    "barangay": ("PH_Adm4_BgySubMuns_complete.shp.zip", "b7869cfa8ad7bdf86769a77fab5de0836a0cc6bf33508125a15b94360e6dff24"),
}
COUNT = {"town": 1642, "city": 149, "municipality": 1493, "huc": 33,
         "province": 82, "region": 18, "barangay": 42010}
ATTRS = {"town": ("adm3_psgc", "adm3_en"),
         "city": ("adm3_psgc", "adm3_en"),
         "municipality": ("adm3_psgc", "adm3_en"),
         "huc": ("adm3_psgc", "adm3_en"),
         "province": ("adm2_psgc", "adm2_en"),
         "region": ("adm1_psgc", "adm1_en"),
         "barangay": ("adm4_psgc", "adm4_en")}
GEO_TYPES = {"city": "City", "municipality": "Mun", "huc": "City",
             "province": "Prov", "region": "Reg", "barangay": "Bgy"}


def _official(level: str):
    """Read the pinned *Q2 2026* official workbook, not upstream Q3 labels."""
    from scripts.convert_psgc import extract
    from scripts.build_boundaries import BUNDLED_PSGC_JSON
    workbook = BUNDLED_PSGC_JSON.with_name("PSGC-2Q-2026-Publication-Datafile.xlsx")
    if not workbook.is_file():
        raise FileNotFoundError(f"Pinned official PSGC workbook missing: {workbook}")
    rows, counts = extract(workbook)
    if level == "huc":
        def eligible(row):
            cls = str(row.get("city_class", "")).strip().casefold()
            return row.get("geographic_level") == "City" and (
                re.search(r"\bhuc\b|highly\s+urbanized", cls) is not None)
    else:
        selected_kind = {"city": "City", "municipality": "Mun",
                         "region": "Reg", "province": "Prov", "barangay": "Bgy"}[level]
        def eligible(row):
            return row.get("geographic_level") == selected_kind
    selected = [dict(psgc_code=r["psgc_code"], name=r["name"], province="")
                for r in rows if eligible(r)]
    if len(selected) != COUNT[level] or len({r["psgc_code"] for r in selected}) != len(selected):
        if level == "huc":
            classes = Counter(str(r.get("city_class", "")) for r in rows if r.get("geographic_level") == "City")
            raise ValueError(f"Official HUC classification did not yield 33 cities; found {len(selected)}. "
                             f"Workbook city_class values: {dict(classes)}")
        raise ValueError(f"Pinned Q2 2026 workbook expected {COUNT[level]} {level} records, got {len(selected)}")
    return selected, workbook


def validate_level(source: Path, level: str, *, expected_sha: str | None = None):
    """Strict, no-write comparison against official Q2 2026 identities."""
    import geopandas as gpd
    if level not in LEVELS or level == "town":
        raise ValueError(f"Expected a non-town level, got {level!r}")
    expected_sha = expected_sha or ASSETS[level][1]
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    actual = upstream.sha256(source)
    if actual != expected_sha:
        raise ValueError(f"Boundary ZIP checksum mismatch: actual={actual}, pinned={expected_sha}")
    official, workbook = _official(level)
    official_by_code = {r["psgc_code"]: r for r in official}
    frame = upstream._read_shapefile(source)
    code_col, name_col = ATTRS[level]
    needed = {code_col, name_col, "geo_level"}
    if not needed.issubset(frame.columns):
        raise ValueError(f"Missing boundary columns {sorted(needed - set(frame.columns))}")
    if frame.crs is None or not frame.crs.is_projected:
        raise ValueError("Boundary CRS must be declared and projected")
    level_rows = frame.loc[frame["geo_level"].astype(str).str.strip().eq(GEO_TYPES[level])].copy()
    if level == "huc":
        # HUC is not a geometric distinction; use the official workbook.
        level_rows = level_rows.loc[
            level_rows[code_col].map(upstream.code10).isin(official_by_code)
        ].copy()
    if len(level_rows) != COUNT[level]:
        raise ValueError(f"Expected {COUNT[level]} {level} polygons, source contains {len(level_rows)}")
    if level_rows.geometry.isna().any() or level_rows.geometry.is_empty.any():
        raise ValueError(f"{level} source includes empty geometries")
    if not level_rows.geometry.geom_type.isin(("Polygon", "MultiPolygon")).all():
        raise ValueError(f"{level} source contains nonpolygon geometries")
    from .geometry_repair import repair_polygons
    level_rows, geometry_repairs = repair_polygons(level_rows, id_column=code_col, name_column=name_col)
    codes = level_rows[code_col].map(upstream.code10).tolist()
    duplicates = [code for code, count in Counter(codes).items() if count != 1]
    missing = sorted(official_by_code.keys() - set(codes))
    extra = sorted(set(codes) - official_by_code.keys())
    if duplicates or missing or extra:
        raise ValueError(f"{level} PSGC mismatch: duplicate={duplicates[:10]}, missing={missing[:10]}, extra={extra[:10]}")
    conflicts = [(code, str(n).strip(), official_by_code[code]["name"])
                 for code, n in zip(codes, level_rows[name_col])
                 if str(n).strip() != official_by_code[code]["name"]]
    # The upstream source is Q3 2026, the pinned workbook Q2 2026. Q3 had
    # one barangay name correction. Preserve Q2 identity on its *exact PSGC*.
    if conflicts and not (level == "barangay" and len(conflicts) == 1):
        raise ValueError(f"Source names do not match pinned Q2 {level} identities: {conflicts[:12]}")
    if level == "barangay" and conflicts:
        print(f"SOURCE-VINTAGE NOTICE: Q3 upstream vs Q2 official barangay name: {conflicts}", flush=True)
    xmin, ymin, xmax, ymax = level_rows.to_crs("EPSG:4326").total_bounds
    if not (114 < xmin < 130 and 1 < ymin < 24 and 114 < xmax < 130 and 1 < ymax < 24):
        raise ValueError(f"Implausible Philippine extent: {(xmin, ymin, xmax, ymax)}")
    # Keep the pinned Q2 code and name rather than silently switching to Q3 labels.
    normalized = gpd.GeoDataFrame({
        "psgc_code": codes,
        "name": [official_by_code[code]["name"] for code in codes],
    }, geometry=level_rows.geometry.to_numpy(), crs=level_rows.crs)
    roster = [official_by_code[c] for c in sorted(official_by_code)]
    report = {
        "validation": "STRUCTURAL_PASS_NOT_LEGAL_BOUNDARY_CERTIFICATION",
        "boundary_level": level, "official_count": COUNT[level],
        "source_release": upstream.RELEASE_PAGE,
        "source_zip": str(source), "source_zip_sha256": actual,
        "official_workbook": str(workbook), "official_workbook_sha256": upstream.sha256(workbook),
        "source_name_differences": conflicts,
        "source_crs": str(level_rows.crs),
        "geometry_repairs": geometry_repairs,
        "source_is_reconstructed": True,
        "interpretation": "PSGC IDs and polygon validity, not surveyed 2026 boundaries or physical transport reachability",
    }
    if level == "barangay":
        report["geometry_notice"] = "Complete level-4 ZIP approximates four 2024 barangay boundaries; do not treat as surveyed legal polygons"
    return normalized, roster, report


def _save_release(frame, roster, report, workspace: Path, *, replace: bool):
    """Publish a complete release and switch a single pointer *last*."""
    from scripts.centroids_from_boundaries import convert
    root = Path(workspace).expanduser().resolve() / "data"
    level = report["boundary_level"]
    pointer = root / "current_boundaries.json"
    if pointer.exists():
        if not replace:
            raise FileExistsError(f"Boundary selection already exists: {pointer}. Use --replace explicitly.")
        # Refuse to replace a malformed pointer without review.
        from .workspace import _selected
        _selected(root, "current_boundaries.json", {"centroids": "muncity_centroids.shp"})
    rel = Path("boundary_releases") / f"psgc-{level}-{report['source_zip_sha256'][:12]}-{report['official_workbook_sha256'][:12]}"
    dest = root / rel
    if dest.exists():
        raise FileExistsError(f"Existing release directory requires review: {dest}")
    dest.mkdir(parents=True, exist_ok=False)
    gpkg = dest / "boundaries.gpkg"
    points = dest / "muncity_centroids.shp"
    try:
        frame.to_file(gpkg, layer="areas", driver="GPKG", index=False)
        readback = __import__("geopandas").read_file(gpkg, layer="areas")
        if len(readback) != len(frame) or readback["psgc_code"].astype(str).tolist() != frame["psgc_code"].tolist():
            raise ValueError("Boundary GeoPackage roundtrip mismatched PSGC codes")
        point_prov = convert(SimpleNamespace(boundaries=gpkg, layer="areas", output=points,
                              code_column="psgc_code", name_column="name", kind_column=None,
                              kind_values="", method="centroid", expected_count=len(frame)))
        roster_path = dest / "selected_psgc_roster.json"
        roster_path.write_text(json.dumps({"schema":"net-distance-ph/psgc-selected-level/v1",
                            "boundary_level":level,"localities":roster,"source_zip_sha256":report["source_zip_sha256"],
                            "official_workbook_sha256":report["official_workbook_sha256"]},
                            indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
        (dest / "boundary_provenance.json").write_text(
            json.dumps({**report,"created_utc":datetime.now(timezone.utc).isoformat(),
                        "centroids":point_prov},indent=2,ensure_ascii=False)+"\n", encoding="utf-8")
        for ext in (".shp", ".shx", ".dbf", ".prj"):
            if not points.with_suffix(ext).is_file():
                raise ValueError(f"Missing centroid shapefile sidecar {ext}")
        record = {"boundary_level":level,"centroids":(rel / points.name).as_posix(),
                  "roster":(rel / roster_path.name).as_posix(),
                  "roster_sha256": upstream.sha256(roster_path),
                  "provenance":(rel / "boundary_provenance.json").as_posix()}
        root.mkdir(parents=True,exist_ok=True)
        temp_pointer = pointer.with_name(pointer.name+".tmp")
        temp_pointer.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
        os.replace(temp_pointer,pointer)
    except BaseException:
        # Never alter any previously selected release; no incomplete tree is published.
        shutil.rmtree(dest,ignore_errors=True)
        raise
    print(f"PUBLISHED: {len(roster)} official {level} polygons/points in {dest}",flush=True)
    return 0


def run(*, workspace: Path, level: str, source: Path | None = None, download: bool = False,
        publish: bool = False, replace: bool = False) -> int:
    if level not in LEVELS or level == "town":
        raise ValueError(f"Unknown non-town boundary level: {level!r}")
    if (source is None) == (not download):
        raise ValueError("Choose exactly one of --download or --source ZIP")
    if replace and not publish:
        raise ValueError("--replace requires --publish")
    root = Path(workspace).expanduser().resolve()
    archive, expected = ASSETS[level]
    path = (upstream._download(root / "data" / "sources" / "psgc-aligned" / upstream.RELEASE / archive,
                               url=f"https://github.com/faeldon/philippines-psgc-shapefiles/releases/download/{upstream.RELEASE}/{archive}",
                               expected_sha=expected) if download else Path(source))
    frame, roster, report = validate_level(path, level, expected_sha=expected)
    print(f"VALIDATED: {len(roster):,} {level} identities; source {archive} SHA-256 matches", flush=True)
    if not publish:
        print("AUDIT ONLY: no selected boundaries changed", flush=True)
        return 0
    return _save_release(frame, roster, report, root, replace=replace)
