"""Plain, provider-independent administrative and population tables.

Running the model reads only two CSVs. The optional exporter is a one-time
migration from the repository's older pinned machine-readable PSGC derivative;
it never requires Excel, pandas, a live PSA API, or provider-specific headers.
"""
from __future__ import annotations

import csv
from collections import Counter
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re

UNIT_COLUMNS = ("psgc_code", "name", "level", "region_psgc", "province_psgc", "town_psgc")
POP_COLUMNS = ("psgc_code", "population")
LEVELS = {"region", "province", "city", "municipality", "barangay"}
SOURCE_LEVEL = {"Reg":"region", "Prov":"province", "City":"city", "Mun":"municipality", "Bgy":"barangay"}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def code10(v):
    # Plain CSVs are text. GIS readers may return integer-valued numeric DBF
    # types; 10-digit PSGCs are exactly representable by IEEE-754 float64.
    if isinstance(v, bool) or v is None:
        raise ValueError(f"Invalid PSGC code {v!r}")
    if isinstance(v, float):
        if not math.isfinite(v) or not v.is_integer() or v < 0 or v >= 10**10:
            raise ValueError(f"Invalid PSGC code {v!r}")
        v = int(v)
    v = str(v).strip()
    if not re.fullmatch(r"[0-9]{1,10}", v):
        raise ValueError(f"Invalid PSGC code {v!r}; use integer or text")
    return v.zfill(10)


def _read_csv(path, required):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not set(required).issubset(set(reader.fieldnames or ())) or len(set(reader.fieldnames or ())) != len(reader.fieldnames or ()):
            raise ValueError(f"{path} requires unique column headers {', '.join(required)}")
        return list(reader)


def _write_csv(path, columns, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
            wr.writeheader(); wr.writerows(rows)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def export_bundled_defaults(repo, dest, *, overwrite=False, expected_counts=(42010,1642,82,18)):
    """One-time conversion from *machine-readable JSONL*, not an XLSX form.

    Existing normalized tables are never silently overwritten. After export,
    routing works even if the entire original PSA folder is removed.
    """
    repo, dest = Path(repo), Path(dest)
    unit_path, pop_path = dest / "units.csv", dest / "barangay_population.csv"
    if unit_path.exists() or pop_path.exists():
        if not overwrite:
            if unit_path.is_file() and pop_path.is_file():
                return dest
            raise FileExistsError("A partial default dataset exists; fix it before proceeding")
    legacy = repo / "reference" / "psgc" / "psgc_q2_2026_all.jsonl.gz"
    if not legacy.is_file():
        raise FileNotFoundError(f"Missing normalized default tables and one-time legacy JSONL source: {legacy}")
    selected = {}
    with gzip.open(legacy, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            level = SOURCE_LEVEL.get(row.get("geographic_level"))
            if level is None:
                continue
            code = code10(row["psgc_code"])
            if code in selected:
                raise ValueError(f"Duplicate source code {code}")
            selected[code] = row
    regions = {c for c, r in selected.items() if r["geographic_level"] == "Reg"}
    provinces = {c for c, r in selected.items() if r["geographic_level"] == "Prov"}
    towns = {c for c, r in selected.items() if r["geographic_level"] in ("City", "Mun")}
    units, pops = [], []
    for code, r in sorted(selected.items()):
        level = SOURCE_LEVEL[r["geographic_level"]]
        region = code[:2] + "00000000"
        province = code[:5] + "00000"
        # PSGC has nested special administrative patterns. Most barangays
        # resolve to a 7-significant-digit city/municipality code (e.g.
        # 1400102001 -> 1400102000); Manila barangays such as 1380610004
        # pass through a SubMun and resolve to 1380600000 (5 digits).
        # Find the *nearest registered City/Mun ancestor*, never guess the
        # parent from one fixed code width or treat SubMun as a town.
        # The hierarchy is an exporter concern; routing subsequently uses
        # explicit parent columns in units.csv, independently of PSGC layout.
        town = next((candidate for width in range(9, 1, -1)
                     if (candidate := code[:width] + "0" * (10 - width)) in towns), "")
        if region not in regions:
            raise ValueError(f"Missing region for code {code}")
        if level == "barangay" and town not in towns:
            raise ValueError(f"Missing parent city/municipality for barangay {code}")
        if level == "barangay":
            val = str(r.get("population_2024", "")).replace(",", "").strip()
            if not re.fullmatch(r"\d+(?:\.0+)?", val):
                raise ValueError(f"Invalid/missing 2024 barangay population for {code}: {val!r}")
            pops.append({"psgc_code": code, "population": int(val.split(".")[0])})
        units.append({"psgc_code": code, "name": r["name"], "level": level,
                      "region_psgc": region,
                      "province_psgc": province if province in provinces else "",
                      "town_psgc": town if level == "barangay" else (code if level in ("city", "municipality") else "")})
    if (len(pops), len(towns), len(provinces), len(regions)) != tuple(expected_counts):
        raise ValueError("Pinned default PSGC counts changed; refusing to publish")
    _write_csv(unit_path, UNIT_COLUMNS, units)
    _write_csv(pop_path, POP_COLUMNS, pops)
    meta = {"schema":"net-distance-ph/plain-population-dataset/v1",
            "source":"Philippine Statistics Authority PSGC Q2 2026; 2024 barangay population",
            "original_source_sha256":sha256(legacy),
            "units_sha256":sha256(unit_path), "population_sha256":sha256(pop_path),
            "n_units":len(units), "n_barangays":len(pops), "population_total":sum(x["population"] for x in pops)}
    (dest / "provenance.json").write_text(json.dumps(meta,indent=2,ensure_ascii=False)+"\n", encoding="utf-8")
    return dest


def load_plain_dataset(dataset_dir, *, allow_missing_population=False):
    """Provider-independent loader; no imports or access to PSA source files."""
    dataset_dir = Path(dataset_dir).expanduser().resolve()
    unit_file, pop_file = dataset_dir / "units.csv", dataset_dir / "barangay_population.csv"
    units_raw = _read_csv(unit_file, UNIT_COLUMNS)
    pop_raw = _read_csv(pop_file, POP_COLUMNS)
    roster = {}
    for i, row in enumerate(units_raw, 2):
        c = code10(row["psgc_code"])
        if c in roster: raise ValueError(f"Duplicate administrative code {c} at row {i}")
        level = row["level"].strip().lower()
        if level not in LEVELS: raise ValueError(f"Unexpected level {level!r} for code {c}")
        name = row["name"].strip()
        if not name: raise ValueError(f"Empty administrative name for {c}")
        parents = {}
        for col in ("region_psgc", "province_psgc", "town_psgc"):
            val = str(row[col]).strip()
            parents[col] = code10(val) if val else ""
        roster[c] = {"name": name, "level": level, **parents,
                     "geographic_level": {"region":"Reg","province":"Prov","city":"City","municipality":"Mun","barangay":"Bgy"}[level]}
    regions = {c for c,r in roster.items() if r["level"] == "region"}
    provinces = {c for c,r in roster.items() if r["level"] == "province"}
    towns = {c for c,r in roster.items() if r["level"] in ("city", "municipality")}
    for c,r in roster.items():
        rg, pv, tn = r["region_psgc"], r["province_psgc"], r["town_psgc"]
        if rg not in regions:
            raise ValueError(f"{c} has invalid/missing region_psgc {rg!r}")
        if pv and pv not in provinces:
            raise ValueError(f"{c} has invalid province_psgc {pv!r}")
        if tn and tn not in towns:
            raise ValueError(f"{c} has invalid town_psgc {tn!r}")
        if r["level"] == "barangay" and not tn:
            raise ValueError(f"Barangay {c} has no town_psgc")
        if r["level"] in ("city", "municipality") and tn != c:
            raise ValueError(f"Town {c} must have town_psgc pointing to itself")
        if r["level"] == "barangay":
            t = roster[tn]
            if t["region_psgc"] != rg or t["province_psgc"] != pv:
                raise ValueError(f"Barangay {c} has inconsistent town/region/province parent IDs")
        if pv and roster[pv]["region_psgc"] != rg:
            raise ValueError(f"Unit {c} has province from another region")
    populations = {}
    for i,row in enumerate(pop_raw,2):
        c = code10(row["psgc_code"])
        if c in populations:
            raise ValueError(f"Duplicate population code {c} at row {i}")
        if c not in roster or roster[c]["level"] != "barangay":
            raise ValueError(f"Population code is not a known barangay: {c}")
        value = str(row["population"]).strip()
        if not re.fullmatch(r"\d+", value):
            raise ValueError(f"Invalid population for {c}: {value!r}; require nonnegative integer")
        populations[c] = int(value)
    expected = {c for c,r in roster.items() if r["level"] == "barangay"}
    missing = expected - populations.keys()
    if missing and not allow_missing_population:
        raise ValueError(f"Population missing for {len(missing)} barangays, e.g. {sorted(missing)[:8]}; use an explicit complete table")
    rows=[]
    for code,p in sorted(populations.items()):
        if p <= 0: continue
        u=roster[code]
        rows.append({"psgc_code": code, "town_psgc":u["town_psgc"],
                     "province_psgc":u["province_psgc"],"region_psgc":u["region_psgc"],
                     "population":p,"barangay_name":u["name"]})
    if not rows: raise ValueError("No positive-population barangays")
    weighted_towns={r["town_psgc"] for r in rows}
    missing_towns=towns-weighted_towns
    if missing_towns:
        raise ValueError(f"No positive-population barangays for {len(missing_towns)} towns: {sorted(missing_towns)[:8]}")
    source={"schema":"net-distance-ph/plain-population-dataset/v1","input_format":"plain UTF-8 CSV",
            "units_file":"units.csv","population_file":"barangay_population.csv",
            "units_sha256":sha256(unit_file),"population_sha256":sha256(pop_file),
            "n_units":len(roster),"n_barangays":len(populations),"population_total":sum(x['population'] for x in rows),
            "missing_population_codes":sorted(missing)}
    source_path = dataset_dir / "provenance.json"
    if source_path.is_file():
        try:
            note=json.loads(source_path.read_text(encoding="utf-8"))
            hashes_match = (note.get("units_sha256")==source["units_sha256"] and
                            note.get("population_sha256")==source["population_sha256"])
            source["source_note"] = note.get("source") if hashes_match else "Modified normalized tables: bundled source attribution is no longer certified"
            source["source_note_matches_input_hashes"] = bool(hashes_match)
        except (ValueError,OSError):
            source["source_note"] = "provenance.json could not be parsed"
    return roster, rows, {"source":source,"data_file_sha256":source["population_sha256"],
                          "population_total":source["population_total"],"missing_population_psgc":sorted(missing)}


def export_ocha_crosswalk(repo, dest):
    """One-time optional flat correspondence CSV: current PSGC -> old OCHA P-code.

    The administrative hierarchy and populations still come solely from
    units.csv and barangay_population.csv. Unlike name matching, the source's
    explicit historical correspondence codes are auditable. A duplicate
    historical code is NOT assigned to multiple present-day barangays.
    """
    source=Path(repo)/'reference'/'psgc'/'psgc_q2_2026_all.jsonl.gz'
    target=Path(dest)/'geometry_crosswalk.csv'
    if target.exists():return target
    if not source.is_file():return None
    pairs=[]
    with gzip.open(source,'rt',encoding='utf-8') as fd:
        for line in fd:
            row=json.loads(line)
            if row.get('geographic_level')!='Bgy':continue
            old=str(row.get('correspondence_code','')).strip()
            if not re.fullmatch(r'\d{9,10}',old):continue
            pairs.append((code10(row['psgc_code']),'PH'+old))
    freq=Counter(old for _,old in pairs)
    distinct=[{'psgc_code':c,'ocha_pcode':old} for c,old in pairs if freq[old]==1]
    _write_csv(target,('psgc_code','ocha_pcode'),distinct)
    return target
