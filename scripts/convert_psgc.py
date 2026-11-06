#!/usr/bin/env python3
"""Deterministically extract the official PSA PSGC worksheet into JSON files.

Input: the unmodified June 2026 PSA publication XLSX. Output: a complete,
compressed JSON Lines copy of the PSGC worksheet plus the 1,642-locality
routing roster. All identifiers remain strings, including leading zeros.
No external spreadsheet dependency is necessary for this conversion.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SOURCE_URL = "https://psa.gov.ph/system/files/scd/PSGC-2Q-2026-Publication-Datafile.xlsx"
COLS = {
    "A": "psgc_code", "B": "name", "C": "correspondence_code", "D": "geographic_level",
    "E": "old_names", "F": "city_class", "G": "income_classification",
    "H": "urban_rural", "I": "population_2024", "K": "status",
}
LEVELS = {"Reg", "Prov", "City", "Mun", "SubMun", "Bgy"}
SOURCE_NAME = "PSGC-2Q-2026-Publication-Datafile.xlsx"
LOCALITIES_NAME = "psgc_q2_2026_localities.json"
ALL_NAME = "psgc_q2_2026_all.jsonl.gz"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for part in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def _strings(archive: zipfile.ZipFile):
    values = []
    with archive.open("xl/sharedStrings.xml") as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag == NS + "si":
                values.append("".join(n.text or "" for n in element.iter(NS + "t")))
                element.clear()
    return values


def extract(source: Path):
    """Extract all PSGC-coded rows, not just municipalities."""
    with zipfile.ZipFile(source) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = [s.attrib.get("name") for s in workbook.iter(NS + "sheet")]
        if sheets[:4] != ["Metadata", "National Summary", "Prov Sum", "PSGC"]:
            raise ValueError(f"Unexpected workbook sheets/order: {sheets!r}")
        strings = _strings(archive)
        records = []
        seen = set()
        with archive.open("xl/worksheets/sheet4.xml") as handle:
            for _, element in ET.iterparse(handle, events=("end",)):
                if element.tag != NS + "row":
                    continue
                row_number = int(element.attrib["r"])
                if row_number == 1:
                    element.clear()
                    continue
                fields = {}
                for cell in element.findall(NS + "c"):
                    col = re.match(r"[A-Z]+", cell.attrib.get("r", ""))
                    if not col or col.group() not in COLS:
                        continue
                    v = cell.find(NS + "v")
                    if v is None or v.text is None:
                        continue
                    value = strings[int(v.text)] if cell.get("t") == "s" else v.text
                    fields[COLS[col.group()]] = value.strip()
                element.clear()
                if not fields:
                    continue
                code = fields.get("psgc_code", "")
                level = fields.get("geographic_level", "")
                if not code and not level:
                    continue
                if not re.fullmatch(r"[0-9]{10}", code) or (level and level not in LEVELS):
                    raise ValueError(f"Unexpected PSGC record at Excel row {row_number}: {fields!r}")
                if not level:
                    # PSA has two unclassified grouping records (Isabela City and SGA).
                    # Preserve them in full JSONL without inventing a level.
                    fields["source_level_missing"] = True
                if code in seen:
                    raise ValueError(f"Duplicate PSGC code {code!r} at Excel row {row_number}")
                if not fields.get("name"):
                    raise ValueError(f"Missing geographic name at Excel row {row_number}")
                seen.add(code)
                records.append({"excel_row": row_number, **fields})
    counts = Counter(row.get("geographic_level") for row in records if row.get("geographic_level"))
    expected = {"Reg": 18, "Prov": 82, "City": 149, "Mun": 1493, "SubMun": 14, "Bgy": 42010}
    if dict(counts) != expected or len(records) != 43768 or sum("source_level_missing" in r for r in records) != 2:
        raise ValueError(f"Unexpected 2026 Q2 PSGC counts or unclassified entries: {dict(counts)}, rows={len(records)}")
    return records, dict(counts)


def render(source: Path):
    source = Path(source)
    records, counts = extract(source)
    provinces = {r["psgc_code"][:5]: r["name"] for r in records if r.get("geographic_level") == "Prov"}
    locality_rows = []
    for row in records:
        if row.get("geographic_level") not in ("City", "Mun"):
            continue
        code = row["psgc_code"]
        locality_rows.append({
            "psgc_code": code,
            "name": row["name"],
            "kind": "city" if row["geographic_level"] == "City" else "municipality",
            "province": provinces.get(code[:5], ""),
        })
    if len(locality_rows) != 1642 or len(set(r["psgc_code"] for r in locality_rows)) != 1642:
        raise ValueError("Expected 1,642 unique cities/municipalities")
    payload = {
        "schema": "net-distance-ph/psgc-localities/v1",
        "source": {
            "agency": "Philippine Statistics Authority",
            "publication_date": "2026-06-30",
            "source_url": SOURCE_URL,
            "filename": SOURCE_NAME,
            "sha256": sha256(source),
            "sheet": "PSGC",
            "source_note": "Provinces derived from Prov records sharing the first five PSGC digits; blank if not represented.",
        },
        "counts": {"city": 149, "municipality": 1493, "total": 1642},
        "localities": locality_rows,
    }
    localities_json = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    data = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=data, mtime=0, compresslevel=9) as gz:
        for row in records:
            gz.write((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return localities_json, data.getvalue(), payload, counts


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parents[1] / "reference" / "psgc"
    p.add_argument("--source", type=Path, default=here / SOURCE_NAME)
    p.add_argument("--output", type=Path, default=here)
    p.add_argument("--check", action="store_true", help="verify existing generated JSON artifacts against XLSX")
    args = p.parse_args(argv)
    localities, all_rows, payload, counts = render(args.source)
    targets = {args.output / LOCALITIES_NAME: localities, args.output / ALL_NAME: all_rows}
    if args.check:
        for path, expected in targets.items():
            if not path.is_file() or path.read_bytes() != expected:
                p.error(f"Generated PSGC dataset is missing or differs from source: {path}")
        print("PSGC conversion verified: 43,768 worksheet records (two unclassified), 1,642 localities, exact source SHA-256")
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    for path, data in targets.items():
        path.write_bytes(data)
        print(f"Wrote: {path} ({len(data):,} bytes)")
    print(f"Validated source {payload['source']['sha256']}; counts {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
