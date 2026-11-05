"""Fail early on ambiguous source snapshots instead of silently mixing files."""


import pytest

import config
from run import _find


def test_find_rejects_ambiguous_basename(tmp_path, monkeypatch):
    for dirname in ("snapshot_1", "snapshot_2"):
        folder = tmp_path / dirname
        folder.mkdir()
        (folder / "primsectertrunk_road.shp").write_bytes(b"fixture")
    monkeypatch.setattr(config, "DATA", tmp_path)
    with pytest.raises(SystemExit, match="Ambiguous primsectertrunk_road.shp"):
        _find("primsectertrunk_road.shp")


def test_find_returns_single_file(tmp_path, monkeypatch):
    folder = tmp_path / "input"
    folder.mkdir()
    expected = folder / "ferry_route_shape.shp"
    expected.write_bytes(b"fixture")
    monkeypatch.setattr(config, "DATA", tmp_path)
    assert _find("ferry_route_shape.shp") == expected
