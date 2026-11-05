"""The optional converter must preserve locality IDs and provenance."""

import argparse

import pytest


def test_boundary_conversion(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Polygon
    from scripts.centroids_from_boundaries import convert

    frame = gpd.GeoDataFrame(
        {"psgc_code": ["001", "002", "999"],
         "name": ["Town A", "Town B", "Province C"],
         "kind": ["municipality", "city", "province"]},
        geometry=[Polygon([(120, 14), (120.1, 14), (120.1, 14.1), (120, 14.1)]),
                  Polygon([(121, 15), (121.1, 15), (121.1, 15.1), (121, 15.1)]),
                  Polygon([(119, 13), (119.2, 13), (119.2, 13.2), (119, 13.2)])],
        crs="EPSG:4326",
    )
    source = tmp_path / "areas.gpkg"
    target = tmp_path / "muncity_centroids.shp"
    frame.to_file(source, layer="areas", driver="GPKG")
    args = argparse.Namespace(boundaries=source, layer="areas", output=target,
                              code_column="psgc_code", name_column="name",
                              kind_column="kind", kind_values="city,municipality",
                              method="centroid", expected_count=2)
    manifest = convert(args)
    result = gpd.read_file(target)
    assert result["PSGC_CODE"].tolist() == ["001", "002"]
    assert result.geometry.geom_type.tolist() == ["Point", "Point"]
    assert len(result) == manifest["count"] == 2
    assert target.with_suffix(".provenance.json").exists()
    with pytest.raises(ValueError, match="Expected 3"):
        convert(argparse.Namespace(**{**vars(args), "expected_count": 3}))


def test_missing_code_is_rejected(tmp_path):
    """With pandas 2.x, astype(str) turned a missing code into the text 'None'."""
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Polygon
    from scripts.centroids_from_boundaries import convert

    frame = gpd.GeoDataFrame(
        {"psgc_code": ["0100101000", None], "name": ["Town A", "Town B"]},
        geometry=[Polygon([(120, 14), (120.1, 14), (120.1, 14.1)]),
                  Polygon([(121, 15), (121.1, 15), (121.1, 15.1)])],
        crs="EPSG:4326",
    )
    source = tmp_path / "areas.gpkg"
    frame.to_file(source, layer="areas", driver="GPKG")
    args = argparse.Namespace(boundaries=source, layer="areas", output=tmp_path / "pts.shp",
                              code_column="psgc_code", name_column="name", kind_column=None,
                              kind_values="city,municipality", method="centroid", expected_count=None)
    with pytest.raises(ValueError, match="nonempty"):
        convert(args)
