"""Validate a pinned PSGC-aligned ADM3 release and optionally publish it.

The third-party administrative geometry is not certified as current legal
boundaries. This module implements structural checks and preserves lineage.
No network access occurs without --download and no selected release changes
without --publish.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.request import Request, urlopen
from zipfile import ZipFile

RELEASE = "v1.0.1"
ARCHIVE_NAME = "PH_Adm3_MuniCities.shp.zip"
DOWNLOAD_URL = (
    "https://github.com/faeldon/philippines-psgc-shapefiles/"
    f"releases/download/{RELEASE}/{ARCHIVE_NAME}"
)
RELEASE_PAGE = "https://github.com/faeldon/philippines-psgc-shapefiles/releases/tag/v1.0.1"
ARCHIVE_SHA256 = "911d025e6da37e55d9cf04c872b1acaf9a6302487a3f0c0fb68d683d9bc982b3"
SCHEMA = "net-distance-ph/psgc-localities/v1"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for piece in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def code10(value: object) -> str:
    if value is None or isinstance(value, bool):
        raise ValueError(f"Invalid PSGC value: {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Noninteger PSGC value: {value!r}")
        value = int(value)
    s = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,10}", s):
        raise ValueError(f"Invalid numeric PSGC code: {value!r}")
    return s.zfill(10)


def _download(cache: Path, *, url: str = DOWNLOAD_URL, expected_sha: str = ARCHIVE_SHA256) -> Path:
    """Download only the exact pinned archive, atomically; never trust an unverified cache."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        if sha256(cache) != expected_sha:
            raise ValueError(f"Cached ZIP checksum differs from pinned release: {cache}. Remove the bad file manually.")
        print(f"REUSE: checksum-verified archive {cache}", flush=True)
        return cache
    tmp = cache.with_name(cache.name + ".part")
    if tmp.exists():
        raise FileExistsError(f"Partial download already exists: {tmp}. Remove it to retry.")
    print(f"DOWNLOADING: {url}\nDestination: {cache}", flush=True)
    req = Request(url, headers={"User-Agent": "net-distance-ph/0.1 (research boundary provenance)"})
    try:
        with urlopen(req, timeout=90) as source, tmp.open("xb") as dest:
            count = 0
            for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
                dest.write(chunk)
                count += len(chunk)
                if count // (32 * 1024 * 1024) != (count - len(chunk)) // (32 * 1024 * 1024):
                    print(f"  downloaded {count / (1024 * 1024):.1f} MiB", flush=True)
        if sha256(tmp) != expected_sha:
            raise ValueError("Downloaded archive checksum is incorrect; refusing to use it")
        os.replace(tmp, cache)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    print("DOWNLOAD PASS: pinned SHA-256 verified", flush=True)
    return cache


def _read_shapefile(zip_path: Path):
    import geopandas as gpd
    with tempfile.TemporaryDirectory(prefix="ndph_psgc_source_") as temp:
        root = Path(temp)
        with ZipFile(zip_path) as archive:
            entries = {Path(n).name.lower(): n for n in archive.namelist() if not n.endswith("/")}
            shp_names = [s for s in entries if s.endswith(".shp")]
            if len(shp_names) != 1:
                raise ValueError(f"Expected exactly one .shp in the ZIP; found {shp_names}")
            stem = Path(shp_names[0]).stem
            for ext in ("shp", "shx", "dbf", "prj", "cpg"):
                name = stem + "." + ext
                if name not in entries:
                    raise ValueError(f"Missing required shapefile member {name}")
                with archive.open(entries[name]) as src, (root / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return gpd.read_file(root / shp_names[0])


def validate(source: Path, roster: Path, *, expected_sha: str = ARCHIVE_SHA256):
    """Return normalized GeoDataFrame and audit report; write nothing."""
    import geopandas as gpd

    source = Path(source).expanduser().resolve()
    roster = Path(roster).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source archive not found: {source}")
    if not roster.is_file():
        raise FileNotFoundError(f"Pinned official roster not found: {roster}")
    actual_sha = sha256(source)
    if actual_sha != expected_sha:
        raise ValueError(f"ZIP SHA-256 mismatch; actual={actual_sha}; expected={expected_sha}")
    data = json.loads(roster.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError("Unexpected pinned official PSGC roster schema")
    official_rows = data.get("localities", [])
    official = {row["psgc_code"]: row for row in official_rows}
    if len(official) != 1642 or len(official_rows) != 1642:
        raise ValueError("Expected 1,642 distinct official municipalities and cities")
    if any(not re.fullmatch(r"[0-9]{10}", code) for code in official):
        raise ValueError("Official PSGC roster contains an invalid code")

    frame = _read_shapefile(source)
    required = {"adm3_psgc", "adm3_en", "geo_level"}
    if not required.issubset(frame.columns):
        raise ValueError(f"ADM3 attributes missing: {sorted(required - set(frame.columns))}")
    if frame.crs is None or not frame.crs.is_projected:
        raise ValueError("ADM3 shapefile CRS must be declared and projected")
    if frame.empty or len(frame) != 1642:
        raise ValueError(f"Expected 1,642 polygons; found {len(frame)}")
    if frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise ValueError("ADM3 contains missing or empty geometry")
    if not frame.geometry.geom_type.isin(("Polygon", "MultiPolygon")).all():
        raise ValueError("ADM3 contains nonpolygon geometry")
    from .geometry_repair import repair_polygons
    frame, geometry_repairs = repair_polygons(frame, id_column="adm3_psgc", name_column="adm3_en")

    codes = [code10(raw) for raw in frame["adm3_psgc"].tolist()]
    c = Counter(codes)
    duplicates = [code for code, count in c.items() if count != 1]
    missing = sorted(official.keys() - c.keys())
    extra = sorted(c.keys() - official.keys())
    if missing or extra or duplicates:
        raise ValueError(f"PSGC coverage failure: missing={missing[:12]}; extra={extra[:12]}; duplicate={duplicates[:12]}")
    name_conflicts, kind_conflicts = [], []
    for code, source_name, source_kind in zip(codes, frame["adm3_en"], frame["geo_level"]):
        row = official[code]
        if str(source_name).strip() != row["name"]:
            name_conflicts.append((code, str(source_name), row["name"]))
        if str(source_kind).strip() != {"city":"City", "municipality":"Mun"}[row["kind"]]:
            kind_conflicts.append((code, str(source_kind), row["kind"]))
    if name_conflicts or kind_conflicts:
        raise ValueError(f"PSGC attribute mismatches: names={name_conflicts[:8]}; kinds={kind_conflicts[:8]}")

    # Check transformed overall extent as a gross CRS/coordinate sanity guard,
    # not as evidence of correct administrative boundaries.
    xmin, ymin, xmax, ymax = frame.to_crs("EPSG:4326").total_bounds
    if not (114 < xmin < 130 and 1 < ymin < 24 and 114 < xmax < 130 and 1 < ymax < 24):
        raise ValueError(f"Implausible Philippine extent (lon/lat): {(xmin, ymin, xmax, ymax)}")

    normalized = gpd.GeoDataFrame({
        "shapeID": codes,
        "psgc_code": codes,
        "shapeName": [str(x).strip() for x in frame["adm3_en"]],
    }, geometry=frame.geometry.to_numpy(), crs=frame.crs)
    report = {
        "validation": "STRUCTURAL_PASS_NOT_LEGAL_BOUNDARY_CERTIFICATION",
        "source_release": RELEASE_PAGE,
        "source_filename": ARCHIVE_NAME,
        "source_zip": str(source),
        "source_zip_sha256": actual_sha,
        "official_roster": str(roster),
        "official_roster_sha256": sha256(roster),
        "official_psgc_publication_date": data.get("source", {}).get("publication_date"),
        "source_crs": frame.crs.to_string(),
        "geometry_repairs": geometry_repairs,
        "feature_count": len(frame),
        "distinct_psgc_codes": len(c),
        "manila_psgc": "1380600000",
        "manila_feature_count": c["1380600000"],
        "geo_bounds_lonlat": [float(xmin), float(ymin), float(xmax), float(ymax)],
        "method_limitation": (
            "Unofficial PSGC-aligned polygons reconstructed from older PSA/NAMRIA/OCHA source. "
            "Exact roster identity and geometry validity do not establish contemporary "
            "legal boundary accuracy, ferry service, or road-network reachability."
        ),
    }
    return normalized, report


def _write_review(frame, report: dict, output: Path) -> Path:
    """Save the normalized source with checksum + provenance; safely reuse it."""
    import geopandas as gpd

    output = Path(output)
    record = output.with_suffix(".provenance.json")
    if output.exists() or record.exists():
        if not output.is_file() or not record.is_file():
            raise FileExistsError("Incomplete prior boundary review; inspect or remove manually")
        old = json.loads(record.read_text(encoding="utf-8"))
        if (old.get("source_zip_sha256") != report["source_zip_sha256"]
                or old.get("official_roster_sha256") != report["official_roster_sha256"]
                or old.get("normalized_gpkg_sha256") != sha256(output)):
            raise ValueError("Existing review GeoPackage provenance mismatch; refusing reuse")
        reviewed = gpd.read_file(output)
        if len(reviewed) != len(frame) or reviewed["psgc_code"].astype(str).tolist() != frame["psgc_code"].tolist():
            raise ValueError("Existing review GeoPackage attributes differ; refusing reuse")
        print(f"REUSE: verified review GeoPackage {output}", flush=True)
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.stem + ".tmp.gpkg")
    if tmp.exists():
        raise FileExistsError(f"Partial review GeoPackage exists: {tmp}")
    try:
        frame.to_file(tmp, driver="GPKG")
        readback = gpd.read_file(tmp)
        if len(readback) != len(frame) or readback["psgc_code"].astype(str).tolist() != frame["psgc_code"].tolist():
            raise ValueError("GeoPackage roundtrip disagrees with validated PSGC rows")
        os.replace(tmp, output)
        details = {**report, "normalized_gpkg": str(output), "normalized_gpkg_sha256": sha256(output)}
        tmp_prov = record.with_suffix(".json.tmp")
        try:
            tmp_prov.write_text(json.dumps(details, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp_prov, record)
        finally:
            tmp_prov.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"SAVED: reviewed ADM3 source {output}", flush=True)
    return output


def run(*, workspace: Path, source: Path | None = None, download: bool = False,
        publish: bool = False, replace: bool = False, level: str = "town") -> int:
    """Run source validation and optionally publish the selected geographic level."""
    if level != "town":
        from .boundary_levels import run as run_level
        return run_level(workspace=workspace, level=level, source=source,
                         download=download, publish=publish, replace=replace)
    from scripts.build_boundaries import BUNDLED_PSGC_JSON, main as build_boundaries

    root = Path(workspace).expanduser().resolve()
    if source is not None and download:
        raise ValueError("Choose --source or --download, not both")
    if source is None and not download:
        raise ValueError("Provide --source ZIP or --download")
    if replace and not publish:
        raise ValueError("--replace requires --publish")
    path = (_download(root / "data" / "sources" / "psgc-aligned" / RELEASE / ARCHIVE_NAME)
            if download else Path(source))
    frame, report = validate(path, BUNDLED_PSGC_JSON)
    print(f"VALIDATION PASS: {report['feature_count']:,} distinct PSGC localities, one Manila city polygon", flush=True)
    print("CAUTION: Source is reconstructed, not certified 2026 legal geometry.", flush=True)
    if not publish:
        print("AUDIT ONLY: no boundary release published. Use --publish after reviewing source provenance.", flush=True)
        return 0

    gpkg = root / "data" / "boundary_review" / f"adm3_psgc_{RELEASE}_{report['source_zip_sha256'][:12]}.gpkg"
    gpkg = _write_review(frame, report, gpkg)
    opts = ["--adm3", str(gpkg), "--skip-adm2", "--output", str(root / "data")]
    if replace:
        opts.append("--replace")
    print("PUBLISH: invoking existing boundary builder with verified PSGC-aligned ADM3 source", flush=True)
    code = build_boundaries(opts)
    if code != 0:
        print("BOUNDARY BUILD STOPPED: no new complete boundary release was selected", file=sys.stderr)
        return 2
    print(f"BOUNDARY RELEASE READY: {root / 'data' / 'current_boundaries.json'}", flush=True)
    print("Next: net-distance-ph run --until connectivity --verbose", flush=True)
    return 0
