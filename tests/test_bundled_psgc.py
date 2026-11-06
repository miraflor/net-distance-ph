"""Pinned official Q2 2026 PSGC source and machine-readable conversion."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from scripts.convert_psgc import render

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reference" / "psgc" / "PSGC-2Q-2026-Publication-Datafile.xlsx"
LOCALITIES = SOURCE.parent / "psgc_q2_2026_localities.json"
ALL = SOURCE.parent / "psgc_q2_2026_all.jsonl.gz"
EXPECTED_HASH = "31892bc2bdde3ea0682562d9412b5bab4d45a0be5e5a5b4f6c9d7714b94bca5d"


def test_workbook_is_pinned_and_json_matches():
    assert SOURCE.is_file()
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == EXPECTED_HASH
    localities, all_records, payload, counts = render(SOURCE)
    assert LOCALITIES.read_bytes() == localities
    assert ALL.read_bytes() == all_records
    assert counts["City"] == 149 and counts["Mun"] == 1493
    assert payload["source"]["sha256"] == EXPECTED_HASH


def test_localities_are_exact_text_codes():
    payload = json.loads(LOCALITIES.read_text(encoding="utf-8"))
    rows = payload["localities"]
    assert len(rows) == 1642
    assert sum(row["kind"] == "city" for row in rows) == 149
    assert sum(row["kind"] == "municipality" for row in rows) == 1493
    assert len(set(row["psgc_code"] for row in rows)) == len(rows)
    assert all(isinstance(row["psgc_code"], str) and len(row["psgc_code"]) == 10 for row in rows)
    assert any(row["psgc_code"].startswith("0") for row in rows)


def test_full_conversion_keeps_unclassified_source_rows():
    with gzip.open(ALL, "rt", encoding="utf-8") as fp:
        records = [json.loads(line) for line in fp]
    assert len(records) == 43768
    missing = {row["psgc_code"] for row in records if row.get("source_level_missing")}
    assert missing == {"0990100000", "1999900000"}
    assert sum(row.get("geographic_level") == "Bgy" for row in records) == 42010


def test_builder_uses_checked_in_json_without_download(tmp_path, monkeypatch):
    import scripts.build_boundaries as builder
    assert builder.BUNDLED_PSGC_JSON == LOCALITIES
    assert builder.BUNDLED_PSGC_JSON.exists()
    # No network is needed to read the default pinned source.
    pandas = __import__("pytest").importorskip("pandas")
    roster = builder.read_psgc(builder.BUNDLED_PSGC_JSON)
    assert len(roster) == 1642
    assert roster.psgc_code.str.len().eq(10).all()
    assert roster.kind.value_counts().to_dict() == {"municipality": 1493, "city": 149}
