"""Regression tests for island coordinates and --top selection in OSM gap inspection."""
from __future__ import annotations

import csv

import pytest
from net_distance_ph.osm_gap_inspection import _read_gap_csv

FIELDNAMES = ("component", "town_names", "nearest_main_road_vertex_km",
              "from_lon", "from_lat", "to_lon", "to_lat")


def _gaps(tmp_path, rows):
    path = tmp_path / "component_gaps.csv"
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDNAMES)
        writer.writeheader()
        for name, distance, lon, lat in rows:
            writer.writerow({"component": name, "town_names": name,
                             "nearest_main_road_vertex_km": distance,
                             "from_lon": lon, "from_lat": lat,
                             "to_lon": lon + 0.001, "to_lat": lat + 0.001})
    return path


def test_kalayaan_is_valid_wgs84(tmp_path):
    # Real-world Kalayaan latitude/longitude from national connectivity report.
    path = _gaps(tmp_path, [("C0025", 423.57793, 114.288157, 11.052812)])
    result = _read_gap_csv(path, top=12)
    assert [r["component"] for r in result] == ["C0025"]


def test_top_12_not_blocked_by_unselected_component(tmp_path):
    # The prior code scanned all 29 records before applying --top.
    rows = [(f"C{i:04d}", i + 1, 120.0, 14.0) for i in range(1, 30) if i != 25]
    rows.append(("C0025", 423.57793, 114.288157, 11.052812))
    path = _gaps(tmp_path, rows)
    result = _read_gap_csv(path, top=12)
    assert len(result) == 12
    assert "C0025" not in {r["component"] for r in result}


@pytest.mark.parametrize("lon,lat", [(181.0, 10.0), (120.0, 91.0), (-181.0, 10.0)])
def test_invalid_wgs84_requested_gap_fails(tmp_path, lon, lat):
    path = _gaps(tmp_path, [("C0001", 0.05, lon, lat)])
    with pytest.raises(ValueError, match="Invalid WGS84 longitude/latitude"):
        _read_gap_csv(path, top=1)


def test_unrequested_invalid_geo_does_not_block_nearby_inspection(tmp_path):
    path = _gaps(tmp_path, [("C0001", 0.05, 120.0, 14.0),
                            ("C0002", 500.0, 181.0, 14.0)])
    assert [x["component"] for x in _read_gap_csv(path, top=1)] == ["C0001"]
    with pytest.raises(ValueError, match="Invalid WGS84"):
        _read_gap_csv(path, top=2)
