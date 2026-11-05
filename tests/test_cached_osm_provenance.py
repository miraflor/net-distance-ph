"""A cached PBF must not be attributed to a URL that did not supply it."""
from __future__ import annotations

import json
import pytest

from scripts.build_transport import _download

ORIGINAL = "https://download.geofabrik.de/asia/philippines-latest.osm.pbf"
ALTERNATE = "https://download-openstreetmap.fr/another/philippines.osm.pbf"


def test_legacy_cached_pbf_has_unknown_origin(tmp_path):
    cached = tmp_path / "philippines-latest.osm.pbf"
    cached.write_bytes(b"historical snapshot")
    details = _download(ORIGINAL, cached)
    assert details["cached"] is True
    assert details["url"] is None
    assert details["requested_url"] == ORIGINAL
    assert details["acquisition_url_recorded"] is False


def test_known_cached_pbf_refuses_different_source_url(tmp_path):
    cached = tmp_path / "philippines-latest.osm.pbf"
    cached.write_bytes(b"historical snapshot")
    cached.with_suffix(".pbf.acquisition.json").write_text(
        json.dumps({"url": ORIGINAL, "response_headers": {"ETag": "abc"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not the requested"):
        _download(ALTERNATE, cached)
    assert cached.read_bytes() == b"historical snapshot"
    details = _download(ORIGINAL, cached)
    assert details["url"] == ORIGINAL
    assert details["acquisition_url_recorded"] is True


def test_new_download_records_actual_acquisition_source(tmp_path, monkeypatch):
    requests = pytest.importorskip("requests")
    import scripts.build_transport as bt
    class Response:
        url = ORIGINAL
        headers = {"ETag": "source-snapshot"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def raise_for_status(self):
            return None
        def iter_content(self, _chunk_size):
            yield b"test pbf bytes"
    monkeypatch.setattr(requests, "get", lambda *a, **kw: Response())
    dest = tmp_path / "philippines-latest.osm.pbf"
    result = bt._download(ORIGINAL, dest)
    assert dest.read_bytes() == b"test pbf bytes"
    details = json.loads(dest.with_suffix(".pbf.acquisition.json").read_text())
    assert details["url"] == ORIGINAL
    assert details["resolved_url"] == ORIGINAL
    assert result["acquisition_url_recorded"] is True
    reused = bt._download(ORIGINAL, dest)
    assert reused["acquisition_url_recorded"] is True
