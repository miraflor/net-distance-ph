#!/usr/bin/env python3
"""Independently source and reconcile Philippine PSGC localities with ADM3 polygons.

The independent upstream sources have different vintages; unmatched/ambiguous
entries are reported, NEVER silently assigned. Use explicit, reviewed
overrides as needed. Successful builds publish one complete versioned release.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import os
import tempfile
import uuid
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

PSA_Q2_2026 = "https://psa.gov.ph/system/files/scd/PSGC-2Q-2026-Publication-Datafile.xlsx"
BUNDLED_PSGC_JSON = Path(__file__).resolve().parents[1] / "reference" / "psgc" / "psgc_q2_2026_localities.json"
GEOBOUNDARIES_ADM3 = "https://www.geoboundaries.org/api/current/gbOpen/PHL/ADM3/"
GEOBOUNDARIES_ADM2 = "https://www.geoboundaries.org/api/current/gbOpen/PHL/ADM2/"
PSA_COLUMNS = {"code": "psgc_code", "name": "name", "kind": "kind", "province": "province"}
ID_FIELDS = ("shapeID", "ADM3_PCODE", "psgc_code", "PSGC_CODE", "id")
CODE_FIELDS = ("psgc_code", "PSGC_CODE", "ADM3_PCODE", "adm3_pcode", "shapeID")
NAME_FIELDS = ("shapeName", "ADM3_EN", "name", "NAME_3", "mun_name", "municipality")
PROV_FIELDS = ("ADM2_EN", "province", "province_name", "PROVINCE", "adm2_en")
KIND_CITY = {"city", "cty", "huc", "icc", "cc", "highly urbanized city", "independent component city", "component city"}
KIND_MUN = {"mun", "municipality", "municipal"}
KIND_PROV = {"prov", "province"}
ALLOWED_HOSTS = {"psa.gov.ph", "www.geoboundaries.org", "github.com", "raw.githubusercontent.com", "media.githubusercontent.com"}
USER_AGENT = {"User-Agent": "net-distance-ph/0.1 (research reproducibility)"}
INCOMPLETE_DIR = "boundary_build_incomplete"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_name(value: object) -> str:
    """Conservative normalization, *not* fuzzy matching or inferred renaming."""
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"\b(city of|municipality of|city|municipality)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def normalize_code(value: object) -> str | None:
    """Read PSGC text, or the older PH+7 locality code when unambiguous.

    This conversion proposes a candidate ONLY: it must exist in the selected
    official roster. Historical boundary/PSGC changes still require audit.
    """
    s = str(value or "").strip().upper()
    if s.endswith(".0"):
        s = s[:-2]
    if re.fullmatch(r"\d{10}", s):
        return s
    if re.fullmatch(r"PH\d{7}", s):
        return s[2:] + "000"
    return None


def normalize_kind(value: object) -> str | None:
    k = str(value or "").strip().lower()
    return "city" if k in KIND_CITY else "municipality" if k in KIND_MUN else "province" if k in KIND_PROV else None


def _column(frame, preferred: str | None, candidates: tuple[str, ...], *, required=True):
    columns = {re.sub(r"[^a-z0-9]+", "", str(v).casefold()): v for v in frame.columns}
    for candidate in ((preferred,) if preferred else ()) + candidates:
        key = re.sub(r"[^a-z0-9]+", "", candidate.casefold())
        if key in columns:
            return columns[key]
    if required:
        raise ValueError(f"Could not identify column {preferred or candidates}. Actual columns: {list(frame.columns)}")
    return None


def _psgc_raw(path: Path, sheet: str | None):
    import pandas as pd
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != "net-distance-ph/psgc-localities/v1":
            raise ValueError("PSGC JSON must use net-distance-ph/psgc-localities/v1 schema")
        metadata = payload.get("source", {})
        if metadata.get("publication_date") != "2026-06-30":
            raise ValueError("PSGC JSON has an unexpected publication date")
        workbook = path.with_name(str(metadata.get("filename", "")))
        if not workbook.is_file() or sha256(workbook) != metadata.get("sha256"):
            raise ValueError("PSGC JSON does not match its original checked-in XLSX (SHA-256)")
        records = payload.get("localities")
        counts = payload.get("counts", {})
        if (not isinstance(records, list) or len(records) != 1642 or
                counts.get("city") != 149 or counts.get("municipality") != 1493 or
                counts.get("total") != 1642):
            raise ValueError("PSGC JSON locality counts are inconsistent")
        if any(not isinstance(row, dict) or
               not {"psgc_code", "name", "kind", "province"}.issubset(row)
               for row in records):
            raise ValueError("PSGC JSON locality record has missing fields")
        return pd.DataFrame(records, dtype=str)
    if path.suffix.lower() == ".csv":
        # Never infer codes as integers: leading zeros are meaningful.
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if path.suffix.lower() not in {".xlsx", ".xls"}:
        raise ValueError("PSGC source must be .csv, .xlsx, or .xls")
    sheets = pd.read_excel(path, sheet_name=sheet if sheet else None, dtype=str, header=None, keep_default_na=False)
    if not isinstance(sheets, dict):
        sheets = {sheet or "sheet": sheets}
    # Many official PSA releases have several title rows before actual headers.
    for raw in sheets.values():
        for header_row in range(min(60, len(raw))):
            row = [str(x).strip().casefold() for x in raw.iloc[header_row].tolist()]
            has_code = any("psgc" in x or "10-digit" in x or "10 digit" in x for x in row)
            has_name = any("name" in x or "geographic area" in x for x in row)
            if has_code and has_name:
                frame = raw.iloc[header_row + 1:].copy()
                frame.columns = [str(x).strip() for x in raw.iloc[header_row].tolist()]
                frame = frame.loc[:, ~frame.columns.duplicated()].reset_index(drop=True)
                return frame
    raise ValueError(
        "Could not locate the PSGC table header in the workbook. Export the official "
        "city/municipality table to CSV with columns psgc_code,name,kind,province "
        "and rerun; no codes or names were guessed."
    )


def read_psgc(path: Path, sheet: str | None = None):
    """Return an auditable 10-digit PSGC city/municipality roster."""
    import pandas as pd
    raw = _psgc_raw(path, sheet)
    code_col = _column(raw, None, ("psgc_code", "10 Digit Code", "10-digit PSGC", "10-digit code", "PSGC", "Code"))
    name_col = _column(raw, None, ("name", "area_name", "geographic area", "geographic name", "Geographic Name", "Name of Area"))
    kind_col = _column(raw, None, ("kind", "geographic_level", "Geographic Level", "level", "type"))
    prov_col = _column(raw, None, ("province", "province_name", "parent_province"), required=False)
    rows = []
    provinces = {}
    for _, r in raw.iterrows():
        rawcode = str(r[code_col]).strip()
        code = normalize_code(rawcode)
        kind = normalize_kind(r[kind_col])
        if kind == "province" and code:
            provinces[code[:5]] = str(r[name_col]).strip()
        if kind not in {"city", "municipality"}:
            continue
        if not code:
            raise ValueError(f"Non-ten-digit PSGC code in locality row: {rawcode!r}")
        name = str(r[name_col]).strip()
        if not name:
            raise ValueError(f"Empty official name for {code}")
        rows.append({"psgc_code": code, "name": name, "kind": kind,
                     "province": str(r[prov_col]).strip() if prov_col is not None else ""})
    if not rows:
        raise ValueError("No cities or municipalities detected. Check PSGC column names and geographic level codes")
    if len(set(x["psgc_code"] for x in rows)) != len(rows):
        raise ValueError("Duplicate PSGC codes in the official roster")
    for item in rows:
        if not item["province"]:
            item["province"] = provinces.get(item["psgc_code"][:5], "")
    return pd.DataFrame(rows, columns=["psgc_code", "name", "kind", "province"])


def _approved(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS


def _save_remote(url: str, path: Path):
    import requests
    if not _approved(url):
        raise ValueError("External download URL is not an approved PSA/geoBoundaries host")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with requests.get(url, timeout=(20, 180), stream=True, allow_redirects=True,
                          headers=USER_AGENT) as response:
            response.raise_for_status()
            # Redirects are followed, so check where the data actually came from.
            if not _approved(response.url):
                raise ValueError(f"Download was redirected to an unapproved host: {response.url}")
            with part.open("wb") as f:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        if part.stat().st_size == 0:
            raise ValueError(f"Downloaded empty file: {url}")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return path


def _geoboundaries(level: str, folder: Path):
    """Return the cached (or newly downloaded) geometry and the metadata that describes it.

    The API path is ``/current/``, so its metadata changes when geoBoundaries
    publishes a new release. The metadata is therefore saved next to the
    geometry when it is downloaded and reused with it, instead of describing
    a cached file with metadata fetched later.
    """
    path = folder / f"geoboundaries_phl_{level.lower()}_simplified.geojson"
    meta_path = folder / f"geoboundaries_phl_{level.lower()}_metadata.json"
    if path.exists() and meta_path.exists():
        return path, json.loads(meta_path.read_text(encoding="utf-8"))
    import requests
    api_url = GEOBOUNDARIES_ADM3 if level == "ADM3" else GEOBOUNDARIES_ADM2
    response = requests.get(api_url, timeout=(20, 60), headers=USER_AGENT)
    response.raise_for_status()
    meta = response.json()
    if meta.get("boundaryISO") != "PHL" or meta.get("boundaryType") != level:
        raise ValueError("geoBoundaries metadata did not match requested PHL administrative layer")
    meta["metadata_retrieved_utc"] = datetime.now(timezone.utc).isoformat()
    if path.exists():
        meta["metadata_warning"] = ("metadata retrieved after the cached geometry file was saved; "
                                    "it may describe a newer geoBoundaries release than the file")
    else:
        # The simplified geometry is intentionally used, matching the historical approach.
        _save_remote(str(meta["simplifiedGeometryGeoJSON"]), path)
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, meta


def read_overrides(path: Path | None):
    if path is None:
        return {}
    overrides = {}
    with path.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if not {"boundary_id", "psgc_code"}.issubset(reader.fieldnames or []):
            raise ValueError("Review overrides CSV must have boundary_id,psgc_code columns")
        for row in reader:
            bid = row["boundary_id"].strip()
            code = normalize_code(row["psgc_code"])
            if not bid or not code:
                raise ValueError(f"Invalid reviewed override: {row}")
            if bid in overrides:
                raise ValueError(f"Duplicate override boundary ID {bid!r}")
            overrides[bid] = code
    return overrides


def _preferred(frame, fields):
    return _column(frame, None, fields, required=False)


def _province_by_overlap(localities, provinces, name_column):
    """Use maximal polygon overlap with ADM2, not centroid containment."""
    if provinces is None:
        return [""] * len(localities)
    if name_column not in provinces:
        raise ValueError(f"ADM2 province layer lacks field {name_column!r}")
    if provinces.crs is None:
        raise ValueError("ADM2 polygons lack declared CRS")
    l = localities.to_crs("EPSG:6933").reset_index(drop=True)
    p = provinces.to_crs("EPSG:6933").reset_index(drop=True)
    idx = p.sindex
    names = []
    for geom in l.geometry:
        if geom is None or geom.is_empty:
            names.append("")
            continue
        areas = [(geom.intersection(p.geometry.iloc[j]).area, str(p.iloc[j][name_column]))
                 for j in idx.query(geom, predicate="intersects")]
        names.append(max(areas)[1] if areas and max(areas)[0] > 0 else "")
    return names


def reconcile(psgc, features, *, overrides=None, inferred_provinces=None, id_col=None, code_col=None, name_col=None, province_col=None):
    """Return matched rows + audit rows. Never use fuzzy best-score guesses."""
    overrides = overrides or {}
    official = {r["psgc_code"]: r for r in psgc.to_dict("records")}
    name_idx = defaultdict(list)
    prov_idx = defaultdict(list)
    for rec in official.values():
        norm = normalize_name(rec["name"])
        name_idx[norm].append(rec["psgc_code"])
        if rec["province"]:
            prov_idx[(norm, normalize_name(rec["province"]))].append(rec["psgc_code"])
    if id_col is None:
        id_col = _preferred(features, ID_FIELDS)
    if code_col is None:
        code_col = _preferred(features, CODE_FIELDS)
    if name_col is None:
        name_col = _preferred(features, NAME_FIELDS)
    if province_col is None:
        province_col = _preferred(features, PROV_FIELDS)
    if name_col is None:
        raise ValueError(f"No locality name column in ADM3 source: {list(features.columns)}")
    if id_col is None:
        # Row IDs are deliberately not used: they are not stable across vintages.
        raise ValueError(f"ADM3 has no stable identifier. Specify a source containing {ID_FIELDS}.")
    matched, audit, seen = [], [], set()
    for i, r in features.reset_index(drop=True).iterrows():
        bid = str(r[id_col]).strip()
        name = str(r[name_col]).strip()
        norm = normalize_name(name)
        province = (str(r[province_col]).strip() if province_col else
                    inferred_provinces[i] if inferred_provinces else "")
        candidate = normalize_code(r[code_col]) if code_col else None
        reason = ""
        matches = []
        if bid in overrides:
            matches = [overrides[bid]]
            reason = "reviewed_override"
        elif candidate and candidate in official:
            matches = [candidate]
            reason = "source_code"
        else:
            if province:
                matches = prov_idx.get((norm, normalize_name(province)), [])
                if matches:
                    reason = "unique_name_and_province"
            if not matches:
                matches = name_idx.get(norm, [])
                reason = "unique_national_name" if len(matches) == 1 else "national_name"
        if len(matches) != 1:
            audit.append({"boundary_id": bid, "boundary_name": name, "boundary_province": province,
                          "suggested_psgc": "|".join(matches),
                          "status": "ambiguous" if matches else "unmatched", "match_basis": reason})
            continue
        code = matches[0]
        if code not in official:
            audit.append({"boundary_id": bid, "boundary_name": name, "boundary_province": province,
                          "suggested_psgc": code, "status": "unknown_override_code", "match_basis": reason})
            continue
        if code in seen:
            audit.append({"boundary_id": bid, "boundary_name": name, "boundary_province": province,
                          "suggested_psgc": code, "status": "duplicate_assignment", "match_basis": reason})
            continue
        # Code-matched records can have an old name; we record this and require
        # review instead of silently publishing a potentially different locality.
        if reason == "source_code" and norm != normalize_name(official[code]["name"]):
            audit.append({"boundary_id": bid, "boundary_name": name, "boundary_province": province,
                          "suggested_psgc": code, "status": "name_conflict_review_required", "match_basis": reason})
            continue
        seen.add(code)
        matched.append((i, code, reason))
        audit.append({"boundary_id": bid, "boundary_name": name, "boundary_province": province,
                      "suggested_psgc": code, "status": "matched", "match_basis": reason})
    for code, item in official.items():
        if code not in seen:
            audit.append({"boundary_id": "", "boundary_name": "", "boundary_province": "",
                          "suggested_psgc": code, "status": "missing_official_locality", "match_basis": item["name"]})
    for bid in overrides:
        if bid not in {str(x).strip() for x in features[id_col]}:
            raise ValueError(f"Override boundary_id {bid!r} is absent from source")
    return matched, audit


def _write_csv(path: Path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def build(args):
    import geopandas as gpd
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    pointer = output / "current_boundaries.json"
    legacy = output / "municipality_boundaries.gpkg"
    # Publishing a new dataset switches a *single pointer*. No reader ever
    # observes a new GeoPackage paired with old point shapefile sidecars.
    if (pointer.exists() or legacy.exists()) and not args.replace:
        raise FileExistsError(
            f"A boundary dataset already exists in {output}. Use --replace to select a new release"
        )
    downloads = output / "sources"
    meta = {}
    if args.psgc:
        psgc_path = args.psgc.expanduser().resolve()
    else:
        # The official PSA XLSX and exact, validated JSON conversion are checked
        # into the repository. Never depend on the blocked live PSA XLSX URL.
        psgc_path = BUNDLED_PSGC_JSON
        if not psgc_path.is_file():
            raise FileNotFoundError(
                f"Pinned PSA Q2 2026 roster missing from repository: {psgc_path}. "
                "Restore reference/psgc or specify --psgc CSV/XLSX/JSON."
            )
    if args.adm3:
        adm3_path = args.adm3.expanduser().resolve()
    else:
        adm3_path, meta["adm3"] = _geoboundaries("ADM3", downloads)
    if args.adm2:
        adm2_path = args.adm2.expanduser().resolve()
    elif args.skip_adm2:
        adm2_path = None
    else:
        adm2_path, meta["adm2"] = _geoboundaries("ADM2", downloads)
    for path in (psgc_path, adm3_path, adm2_path):
        if path is not None and not path.is_file():
            raise FileNotFoundError(path)
    psgc = read_psgc(psgc_path, args.psgc_sheet)
    adm3 = gpd.read_file(adm3_path, layer=args.adm3_layer)
    if adm3.crs is None or adm3.empty:
        raise ValueError("ADM3 has no declared CRS or no polygon features")
    if not adm3.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("ADM3 must contain only polygon geometries")
    if adm3.geometry.isna().any() or adm3.geometry.is_empty.any():
        raise ValueError("ADM3 has empty geometries")
    if not adm3.geometry.is_valid.all():
        raise ValueError("ADM3 has invalid polygons; repair upstream or use a verified source")
    if adm2_path:
        adm2 = gpd.read_file(adm2_path, layer=args.adm2_layer)
        prov_col = args.adm2_province_col or _preferred(adm2, ("shapeName", "ADM2_EN", "name", "NAME_2"))
        if not prov_col:
            raise ValueError(f"No province name column in ADM2: {list(adm2.columns)}")
        inferred = _province_by_overlap(adm3, adm2, prov_col)
    else:
        inferred = None
    id_col = args.boundary_id_col or _preferred(adm3, ID_FIELDS)
    code_col = args.boundary_code_col or _preferred(adm3, CODE_FIELDS)
    name_col = args.boundary_name_col or _preferred(adm3, NAME_FIELDS)
    prov_col = args.boundary_province_col or _preferred(adm3, PROV_FIELDS)
    matched, audit = reconcile(psgc, adm3, overrides=read_overrides(args.overrides),
                               inferred_provinces=inferred, id_col=id_col,
                               code_col=code_col, name_col=name_col, province_col=prov_col)
    audit_columns = ("boundary_id", "boundary_name", "boundary_province",
                     "suggested_psgc", "status", "match_basis")
    counts = Counter(x["status"] for x in audit)
    acceptable = {"matched", "missing_official_locality"}
    if args.allow_extra_boundaries:
        acceptable.add("unmatched")
    complete = len(matched) == len(psgc) and all(x["status"] in acceptable for x in audit)
    provenance = {
        "status": "complete" if complete else "incomplete",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "psgc_source": str(psgc_path), "psgc_sha256": sha256(psgc_path),
        "psgc_original_xlsx_sha256": (json.loads(psgc_path.read_text(encoding="utf-8"))["source"]["sha256"]
                                      if psgc_path.suffix.lower() == ".json" else None),
        "adm3_source": str(adm3_path), "adm3_sha256": sha256(adm3_path),
        "adm2_source": str(adm2_path) if adm2_path else None,
        "adm2_sha256": sha256(adm2_path) if adm2_path else None,
        "overrides_source": str(args.overrides) if args.overrides else None,
        "overrides_sha256": sha256(args.overrides) if args.overrides else None,
        "source_metadata": meta, "official_count": len(psgc),
        "source_adm3_count": len(adm3), "matched_count": len(matched),
        "audit_counts": dict(counts), "boundary_id_column": id_col,
        "boundary_code_column": code_col, "boundary_name_column": name_col,
        "boundary_province_column": prov_col,
        "attribution": "Philippine Statistics Authority PSGC; geoBoundaries, respecting layer-specific license metadata",
    }
    attempt_dir = output / INCOMPLETE_DIR
    if not complete:
        # A failed attempt never touches the published files in ``output``.
        attempt_dir.mkdir(parents=True, exist_ok=True)
        audit_file = attempt_dir / "boundary_match_audit.csv"
        _write_csv(audit_file, audit, audit_columns)
        (attempt_dir / "boundary_provenance.json").write_text(
            json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"INCOMPLETE: matched {len(matched):,}/{len(psgc):,} PSGC localities; "
              f"see {audit_file}. No completed boundary dataset was written, and no "
              f"published file in {output} was changed.", file=sys.stderr)
        return False
    official = psgc.set_index("psgc_code")
    ordered = sorted(matched, key=lambda x: x[1])
    geo = gpd.GeoDataFrame({
        "psgc_code": [code for _, code, _ in ordered],
        "name": [str(official.loc[code, "name"]) for _, code, _ in ordered],
        "kind": [str(official.loc[code, "kind"]) for _, code, _ in ordered],
        "province": [str(official.loc[code, "province"]) for _, code, _ in ordered],
        "source_id": [str(adm3.iloc[i][id_col]) for i, _, _ in ordered],
        "match_basis": [basis for _, _, basis in ordered],
    }, geometry=adm3.geometry.iloc[[i for i, _, _ in ordered]].to_numpy(), crs=adm3.crs)
    geo = geo.to_crs("EPSG:4326")
    # Build *all* artifacts in a private release directory, verify them, then
    # atomically switch one pointer to the complete directory. The prior release
    # remains untouched (also on Windows, where replacing an open directory is
    # not portable). If writing any stage fails, the pointer stays unchanged.
    releases = output / "boundary_releases"
    releases.mkdir(parents=True, exist_ok=True)
    release_id = uuid.uuid4().hex[:16]
    destination = releases / release_id
    with tempfile.TemporaryDirectory(dir=releases, prefix=".staging-") as temp:
        staged = Path(temp)
        staged_boundaries = staged / "municipality_boundaries.gpkg"
        geo.to_file(staged_boundaries, layer="areas", driver="GPKG", index=False)
        provenance["output_boundaries_sha256"] = sha256(staged_boundaries)
        audit_file = staged / "boundary_match_audit.csv"
        _write_csv(audit_file, audit, audit_columns)
        try:
            from scripts.centroids_from_boundaries import convert
        except ModuleNotFoundError as exc:
            if exc.name != "scripts":
                raise
            from centroids_from_boundaries import convert
        points_file = staged / "muncity_centroids.shp"
        converter_meta = convert(argparse.Namespace(
            boundaries=staged_boundaries, layer="areas", output=points_file,
            code_column="psgc_code", name_column="name", kind_column=None,
            kind_values="city,municipality", method=args.point_method,
            expected_count=len(psgc),
        ))
        # The converter initially sees a private staging path. Use the future
        # immutable release path in its published provenance instead.
        converter_meta["source_file"] = str(destination / "municipality_boundaries.gpkg")
        converter_meta["output"] = str(destination / "muncity_centroids.shp")
        points_file.with_suffix(".provenance.json").write_text(
            json.dumps(converter_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        point_files = {}
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".provenance.json"):
            part = points_file.with_suffix(ext)
            if not part.is_file():
                raise ValueError(f"Incomplete centroid shapefile in private release: {part}")
            point_files[ext] = sha256(part)
        provenance["point_files_sha256"] = point_files
        provenance["audit_sha256"] = sha256(audit_file)
        (staged / "boundary_provenance.json").write_text(
            json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(staged, destination)
    base = Path("boundary_releases") / release_id
    record = {
        "schema": 1, "release_id": release_id,
        "boundaries": (base / "municipality_boundaries.gpkg").as_posix(),
        "centroids": (base / "muncity_centroids.shp").as_posix(),
        "audit": (base / "boundary_match_audit.csv").as_posix(),
        "provenance": (base / "boundary_provenance.json").as_posix(),
        "source_fingerprint": provenance["output_boundaries_sha256"],
    }
    temporary_pointer = output / ".current_boundaries.json.tmp"
    try:
        with temporary_pointer.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_pointer, pointer)
    finally:
        temporary_pointer.unlink(missing_ok=True)
    if attempt_dir.exists():
        shutil.rmtree(attempt_dir)  # diagnostics of an earlier failed attempt
    print(f"SUCCESS: {len(geo):,} independently matched boundaries in {destination}")
    print(f"Prepared road-network origins: {destination / 'muncity_centroids.shp'}")
    print(f"Current release selected by: {pointer}")
    return True


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--psgc", type=Path, help="Optional custom PSGC CSV/XLSX or pinned-roster JSON; default uses reference/psgc")
    parser.add_argument("--psgc-sheet", help="Excel worksheet to use; otherwise detect header in any sheet")
    parser.add_argument("--adm3", type=Path, help="Local ADM3 polygons; omit to download geoBoundaries PHL ADM3")
    parser.add_argument("--adm3-layer", help="Layer name for local ADM3 GeoPackage")
    parser.add_argument("--adm2", type=Path, help="Local ADM2 provinces; omit to download geoBoundaries PHL ADM2")
    parser.add_argument("--adm2-layer", help="Layer name for local ADM2 GeoPackage")
    parser.add_argument("--skip-adm2", action="store_true", help="Skip province overlap disambiguation")
    parser.add_argument("--adm2-province-col", help="Province name column in ADM2")
    parser.add_argument("--boundary-id-col", help="Stable source feature identifier column")
    parser.add_argument("--boundary-code-col", help="Source locality code column (if present)")
    parser.add_argument("--boundary-name-col", help="Source locality name column")
    parser.add_argument("--boundary-province-col", help="Source province field (overrides inferred province)")
    parser.add_argument("--overrides", type=Path, help="Reviewed CSV boundary_id,psgc_code to resolve difficult cases")
    parser.add_argument("--output", type=Path, default=Path("data"), help="Separate output and source-cache folder")
    parser.add_argument("--point-method", choices=["centroid", "representative-point"], default="centroid")
    parser.add_argument("--allow-extra-boundaries", action="store_true",
                        help="Permit source polygons outside official PSGC only after reviewing audit (e.g. obsolete ADM3)")
    parser.add_argument("--replace", action="store_true", help="Explicitly replace existing derived boundary GeoPackage")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        return 0 if build(arguments(argv)) else 2
    except (ValueError, FileNotFoundError, FileExistsError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
