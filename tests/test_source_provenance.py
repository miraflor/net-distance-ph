"""SHA-256 provenance must block stale inputs even if size and mtime coincide."""
from __future__ import annotations

import json
import os


from tests.test_end_to_end import _run, build_fixture


def test_input_sidecar_change_is_detected_and_recorded(tmp_path):
    build_fixture(tmp_path / "data")
    result = _run(tmp_path, "all")
    assert result.returncode == 0, result.stdout + result.stderr

    out = tmp_path / "out"
    report = json.loads((out / "matrix_report.json").read_text())
    manifest = json.loads((out / "source_manifest.json").read_text())
    assert report["input_fingerprint"] == manifest["input_manifest"]["fingerprint"]
    assert len(report["input_fingerprint"]) == 64
    assert {"ferries", "roads", "localities"} == set(manifest["input_manifest"]["inputs"])

    # Preserve size + timestamps deliberately; naive provenance would miss it.
    sidecar = tmp_path / "data" / "muncity_centroids_shp" / "muncity_centroids.dbf"
    before = sidecar.stat()
    content = bytearray(sidecar.read_bytes())
    index = content.find(b"Town 1")
    assert index >= 0
    content[index] = ord("B")
    sidecar.write_bytes(content)
    os.utime(sidecar, ns=(before.st_atime_ns, before.st_mtime_ns))

    for stage in ("reduce", "matrix", "verify"):
        result = _run(tmp_path, stage)
        assert result.returncode != 0
        assert "changed since graph construction" in result.stdout + result.stderr

    # Regenerate graph; old reduced core and output are then rejected until
    # their own stages have also been regenerated.
    assert _run(tmp_path, "graph").returncode == 0
    assert _run(tmp_path, "matrix").returncode != 0


def test_pointer_releases_are_selected_without_ambiguity(tmp_path):
    from tests.test_transport_builder import _args
    from scripts.build_transport import build
    from run import _find
    import config
    build(_args(tmp_path))
    build(_args(tmp_path, replace=True))
    old_data = config.DATA
    try:
        config.DATA = tmp_path / "data"
        selected = _find(config.ROAD_SHP)
        current = json.loads((config.DATA / "current_transport.json").read_text())
        assert selected == config.DATA / current["roads"]
    finally:
        config.DATA = old_data
