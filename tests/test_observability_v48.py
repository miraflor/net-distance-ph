"""Offline acceptance tests for v48 resource and scientific observability."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from net_distance_ph import observability as ob
from net_distance_ph import workspace as ws


def test_optional_resource_monitor_never_requires_psutil(monkeypatch):
    # Force an unavailable import without relying on the developer's machine.
    from builtins import __import__ as native_import
    def fake_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("not installed")
        return native_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", fake_import)
    meter = ob.ResourceSampler(interval=0.01)
    assert not meter.available
    meter.start()
    assert "unavailable" in meter.status()
    assert meter.finish()["resource_status"] == "unavailable"


def test_sampled_resources_include_descendant_when_available():
    try:
        import psutil
    except ImportError:
        return
    meter = ob.ResourceSampler(interval=0.03).start()
    time.sleep(0.09)
    metrics = meter.finish()
    assert metrics["resource_status"] == "sampled"
    assert metrics["resource_samples"] >= 2
    assert metrics["peak_process_tree_rss_bytes"] > 0
    assert metrics["peak_observed_processes"] >= 1
    assert metrics["peak_cpu_cores"] >= 0


def test_timed_step_publishes_resource_fields_and_compact_console(tmp_path, capsys):
    with ws.run_log(tmp_path):
        with ws.timed_step("tiny observation", heartbeat_seconds=0.02):
            time.sleep(0.05)
    log = next((tmp_path / "logs").glob("run-*.log"))
    content = log.read_text(encoding="utf-8")
    assert "DONE: tiny observation" in content
    assert "RAM peak" in content or "RAM/CPU unavailable" in content
    assert re.search(r"\[\d{2}:\d{2}:\d{2} e=\d{2}:\d{2}\]", content)
    events_file = next((tmp_path / "logs").glob("run-*.jsonl"))
    records = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    done = next(x for x in records if x["event"] == "phase_finish")
    assert done["resource_status"] in ("sampled", "unavailable")
    assert done["phase"] == "tiny observation"
    assert done["elapsed_seconds"] >= 0.04


def test_evidence_report_on_prior_existing_run(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    console = logs / "run-20261118.log"
    console.write_text("\n".join([
       "[02:06:33] 179 offshore ferry terminals had no road within 5.0 km",
       "[02:09:00] MEDIAN SELECTION: balanced; 10,505 sites",
       "[02:30:23] POPULATION COVERAGE: 112,723,109/112,727,776 residents represented by snapped barangay origins",
       "[02:31:01] VERIFIED: 5 original graph sources against 10490 representative sites; max error 2.046e-11km; 0 reachability mismatches",
       "[02:31:06] PROVINCE ACCOUNTING: 43 towns excluded; population 24,351,876",
    ]), encoding="utf-8")
    report = ob.build_report(tmp_path, [console], status="complete")
    assert report["scientific_validation"] == "not_established"
    assert report["metrics"]["province_excluded_towns"] == 43
    assert report["metrics"]["verification_sources"] == 5
    assert report["metrics"]["excluded_population"] == 4667
    assert report["checks"]["national_component_connectivity_audit"]["status"] == "not_assessed"
    assert report["checks"]["independent_shortest_path_samples"]["status"] == "sample_pass"
    assert report["checks"]["province_aggregation_completeness"]["status"] == "warning"


def test_existing_report_command_does_not_compute_or_modify_matrices(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    prior = logs / "run-20261118.log"
    prior.write_text("[02:09:00] MEDIAN SELECTION: balanced; 10,505 sites\n", encoding="utf-8")
    (logs / "fresh_run_summary.json").write_text(json.dumps({
        "status": "complete", "git_commit": "abcdef", "stages": [{"stage":"population-weighted"}]}))
    output = tmp_path / "output"
    output.mkdir()
    matrix = output / "distance_matrix.npy"
    matrix.write_bytes(b"preserve")
    report = ob.report_existing(tmp_path, write=True)
    assert (logs / "observability_report.json").is_file()
    assert matrix.read_bytes() == b"preserve"
    assert report["metrics"]["representative_sites"] == 10505
    assert report["log_matching"] == "inferred_latest_three"


def test_unknown_checks_remain_unassessed(tmp_path):
    result = ob.build_report(tmp_path, [], status="complete")
    assert result["checks"]["national_component_connectivity_audit"]["status"] == "not_assessed"
    assert result["checks"]["barangay_snapping_coverage"]["status"] == "not_assessed"
    assert result["scientific_validation"] == "not_established"
