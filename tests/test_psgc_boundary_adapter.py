"""Offline contract checks for pinned PSGC ADM3 boundary import.

Synthetic-only tests: these do not download the national source and do not
establish any nationwide geometry, distance, or reachability claim.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from net_distance_ph import boundaries, cli


def test_10_digit_codes_retained():
    assert boundaries.code10("100000000") == "0100000000"
    assert boundaries.code10(1380600000) == "1380600000"
    for raw in ("1e9", "-1", "1.5", None, True):
        with pytest.raises(ValueError):
            boundaries.code10(raw)


def test_archive_pinned():
    assert boundaries.ARCHIVE_NAME == "PH_Adm3_MuniCities.shp.zip"
    assert len(boundaries.ARCHIVE_SHA256) == 64
    assert boundaries.DOWNLOAD_URL.endswith("/v1.0.1/PH_Adm3_MuniCities.shp.zip")


def test_cli_contains_explicit_boundary_command():
    cmd = cli.build_parser().parse_args(["boundaries", "--download"])
    assert cmd.command == "boundaries" and cmd.download and not cmd.publish
    cmd = cli.build_parser().parse_args(["boundaries", "--source", "source.zip", "--publish"])
    assert cmd.publish and cmd.source == Path("source.zip")
    with pytest.raises(SystemExit) as err:
        cli.build_parser().parse_args(["boundaries", "--source", "a", "--download"])
    assert err.value.code == 2


def _fake_roster(path):
    rows = [{"psgc_code": f"{i:010d}", "name": f"Town {i}", "kind": "municipality"} for i in range(1641)]
    rows.append({"psgc_code": "1380600000", "name": "City of Manila", "kind": "city"})
    path.write_text(json.dumps({"schema": boundaries.SCHEMA, "source": {"publication_date": "2026-06-30"},
                                "localities": rows}), encoding="utf-8")
    return rows


def _fake_frame(rows):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box
    # 1,642 small polygons near Manila, projected EPSG:32651.
    polygons = [box(400_000 + (i % 50) * 10, 1_500_000 + (i // 50) * 10,
                    400_005 + (i % 50) * 10, 1_500_005 + (i // 50) * 10)
                for i in range(len(rows))]
    return gpd.GeoDataFrame({"adm3_psgc": [int(row["psgc_code"]) for row in rows],
                             "adm3_en": [row["name"] for row in rows],
                             "geo_level": ["City" if row["kind"] == "city" else "Mun" for row in rows]},
                            geometry=polygons, crs="EPSG:32651")


def test_validation_is_read_only_and_manila_collapsed(tmp_path, monkeypatch):
    roster = tmp_path / "roster.json"
    rows = _fake_roster(roster)
    frame = _fake_frame(rows)
    source = tmp_path / "source.zip"
    source.write_bytes(b"synthetic-placeholder")
    monkeypatch.setattr(boundaries, "_read_shapefile", lambda _: frame)
    result, report = boundaries.validate(source, roster, expected_sha=boundaries.sha256(source))
    assert len(result) == 1642
    assert report["manila_feature_count"] == 1
    assert report["distinct_psgc_codes"] == 1642
    assert not (tmp_path / "boundary_review").exists()
    assert not (tmp_path / "current_boundaries.json").exists()


def test_duplicate_rejected(tmp_path, monkeypatch):
    roster = tmp_path / "roster.json"; rows = _fake_roster(roster)
    frame = _fake_frame(rows)
    frame.loc[0, "adm3_psgc"] = int(rows[1]["psgc_code"])
    source = tmp_path / "source.zip"; source.write_bytes(b"synthetic")
    monkeypatch.setattr(boundaries, "_read_shapefile", lambda _: frame)
    with pytest.raises(ValueError, match="coverage failure"):
        boundaries.validate(source, roster, expected_sha=boundaries.sha256(source))


def test_name_mismatch_rejected(tmp_path, monkeypatch):
    roster = tmp_path / "roster.json"; rows = _fake_roster(roster)
    frame = _fake_frame(rows)
    frame.loc[0, "adm3_en"] = "Different Town"
    source = tmp_path / "source.zip"; source.write_bytes(b"synthetic")
    monkeypatch.setattr(boundaries, "_read_shapefile", lambda _: frame)
    with pytest.raises(ValueError, match="attribute mismatches"):
        boundaries.validate(source, roster, expected_sha=boundaries.sha256(source))


def test_hash_mismatch_fails_without_loading_geometry(tmp_path, monkeypatch):
    roster = tmp_path / "roster.json"; _fake_roster(roster)
    source = tmp_path / "source.zip"; source.write_bytes(b"changed")
    monkeypatch.setattr(boundaries, "_read_shapefile", lambda _: pytest.fail("must refuse ZIP first"))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        boundaries.validate(source, roster)


def test_download_cannot_trust_bad_cache(tmp_path):
    path = tmp_path / "source.zip"
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="Cached ZIP checksum"):
        boundaries._download(path)


def test_publish_opt_in_and_uses_existing_builder(tmp_path, monkeypatch):
    dummy_source = tmp_path / "source.zip"; dummy_source.write_bytes(b"stub")
    fake_report = {"feature_count":1642, "source_zip_sha256":"f" * 64, "official_roster_sha256":"e" * 64}
    calls = []
    monkeypatch.setattr(boundaries, "validate", lambda *args: (object(), fake_report))
    monkeypatch.setattr(boundaries, "_write_review", lambda frame, report, output: (calls.append(("review", output)) or output))
    import scripts.build_boundaries as source_builder
    monkeypatch.setattr(source_builder, "main", lambda args: (calls.append(("builder", args)) or 0))
    assert boundaries.run(workspace=tmp_path, source=dummy_source) == 0
    assert calls == []
    assert boundaries.run(workspace=tmp_path, source=dummy_source, publish=True) == 0
    assert calls[0][0] == "review"
    assert calls[1][0] == "builder"
    assert "--skip-adm2" in calls[1][1]
    assert "--output" in calls[1][1]
    assert "--replace" not in calls[1][1]


def test_cli_boundaries_dispatch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(boundaries, "run", lambda **kw: (calls.append(kw) or 0))
    assert cli.main(["boundaries", "--source", str(tmp_path / "source.zip"), "--workspace", str(tmp_path)]) == 0
    assert len(calls) == 1 and calls[0]["publish"] is False
    assert calls[0]["workspace"] == tmp_path.resolve()
