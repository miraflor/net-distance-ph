"""Opt-in resource sampling and evidence-based run reports.

No routing algorithms, caches, geographic artifacts, or matrices are modified.
Sampling is observational and necessarily misses spikes shorter than the sample
interval. Optional psutil reports process-tree RSS and CPU core equivalents.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import threading
import time


class ResourceSampler:
    """Sample the current Python process and descendants without blocking work.

    CPU numbers are total core-equivalents over a sample interval, NOT a percent
    of one core. RSS is summed over a process tree, so shared pages can be
    counted more than once. Peak sampled memory is a lower bound on true peak.
    """

    def __init__(self, interval=2.0, psutil_module=None):
        self.interval = float(interval)
        if not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError("Resource sample interval must be positive")
        if psutil_module is None:
            try:
                import psutil as psutil_module
            except (ImportError, OSError):
                psutil_module = None
        self.psutil = psutil_module
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._previous_cpu = {}
        self._previous_time = None
        self.samples = 0
        self.peak_rss_bytes = 0
        self.peak_cpu_cores = 0.0
        self.peak_processes = 0
        self._current_rss_bytes = 0
        self._current_cpu_cores = 0.0
        self._sampling_errors = 0
        self._origin = time.monotonic()

    @property
    def available(self):
        return self.psutil is not None

    def _sample(self):
        if self.psutil is None:
            return
        try:
            parent = self.psutil.Process(os.getpid())
            processes = [parent, *parent.children(recursive=True)]
        except Exception:
            with self._lock:
                self._sampling_errors += 1
            return
        rss = 0
        cpu = {}
        seen = 0
        for proc in processes:
            try:
                # creation time disambiguates recycled PIDs; best effort only.
                identity = (proc.pid, proc.create_time())
                mem = proc.memory_info().rss
                times = proc.cpu_times()
                seconds = float(times.user) + float(times.system)
                if not (math.isfinite(seconds) and seconds >= 0):
                    continue
                rss += max(0, int(mem))
                cpu[identity] = seconds
                seen += 1
            except Exception:
                continue
        now = time.monotonic()
        with self._lock:
            if self._previous_time is not None and now > self._previous_time:
                consumed = sum(max(0, total - self._previous_cpu.get(pid, total))
                               for pid, total in cpu.items())
                cores = consumed / (now - self._previous_time)
                self._current_cpu_cores = cores
                self.peak_cpu_cores = max(self.peak_cpu_cores, cores)
            self._previous_cpu = cpu
            self._previous_time = now
            self._current_rss_bytes = rss
            self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
            self.peak_processes = max(self.peak_processes, seen)
            self.samples += 1

    def start(self):
        if self.psutil is None:
            return self
        self._sample()
        def loop():
            while not self._stop.wait(self.interval):
                self._sample()
        self._thread = threading.Thread(target=loop, name="net-distance-ph-resources", daemon=True)
        self._thread.start()
        return self

    def status(self):
        if self.psutil is None:
            return "RAM/CPU unavailable (install psutil)"
        with self._lock:
            gib = self.peak_rss_bytes / (1024 ** 3)
            return f"RAM peak {gib:.2f} GiB | CPU {self._current_cpu_cores:.1f} cores"

    def finish(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
        self._sample()
        with self._lock:
            if self.psutil is None:
                return {"resource_status": "unavailable", "resource_reason": "psutil not installed"}
            cpu_cores = round(self.peak_cpu_cores, 3)
            cpu_count = os.cpu_count()
            return {
                "resource_status": "sampled",
                "resource_sampling_interval_seconds": self.interval,
                "resource_samples": self.samples,
                "peak_process_tree_rss_bytes": self.peak_rss_bytes,
                "peak_cpu_cores": cpu_cores,
                "peak_machine_cpu_percent": round(100 * cpu_cores / cpu_count, 2) if cpu_count else None,
                "peak_observed_processes": self.peak_processes,
                "sampling_errors": self._sampling_errors,
            }


# Explicit evidence extraction from this project's existing console messages.
# Checks that were not performed stay "not_assessed"; no inference from exit=0.
_PATTERNS = {
    "population": re.compile(r"POPULATION COVERAGE:\s*([\d,]+)/([\d,]+) residents"),
    "province": re.compile(r"PROVINCE ACCOUNTING:\s*([\d,]+) towns excluded; population ([\d,]+)"),
    "ferry": re.compile(r"([\d,]+) offshore ferry terminals had no road within ([\d.]+) km"),
    "verify": re.compile(r"VERIFIED:\s*([\d,]+) original graph sources against ([\d,]+) representative sites; max error\s*([\deE+.-]+)\s*km;\s*([\d,]+) reachability mismatches"),
    "sites": re.compile(r"MEDIAN SELECTION:\s*([\w-]+);\s*([\d,]+) sites"),
    "graph": re.compile(r"([\d,]+) vertices -> ([\d,]+) nodes"),
}


def _parse(log_files):
    found = {}
    for path in log_files:
        path = Path(path)
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as fd:
            for line in fd:
                for name, expression in _PATTERNS.items():
                    match = expression.search(line)
                    if match:
                        found[name] = (match.groups(), str(path))
    return found


def _int(value):
    return int(value.replace(",", ""))


def _provenance(workspace):
    data = Path(workspace) / "data"
    sources = {}
    for key, filename in (("boundaries", "current_boundaries.json"), ("transport", "current_transport.json")):
        path = data / filename
        if path.is_file():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                # Only known source-identification fields; do not copy paths or sensitive secrets.
                sources[key] = {k: record[k] for k in ("release_id", "source_fingerprint", "source_sha256") if k in record}
            except (ValueError, OSError):
                sources[key] = {"status": "unreadable"}
    return sources


def build_report(workspace, log_files, *, status="unknown", stages=None, phases=None, git_commit=None):
    """Return a transparent evidence inventory, not a research certification."""
    found = _parse(log_files)
    checks = {}
    metrics = {}
    checks["execution"] = {"status": "completed" if status == "complete" else status,
                           "meaning": "Exit status only; not geographic validity"}
    if "population" in found:
        (num, den), path = found["population"]
        n, d = _int(num), _int(den)
        metrics["represented_population"] = n
        metrics["input_population"] = d
        metrics["excluded_population"] = d - n
        metrics["population_coverage_fraction"] = round(n / d, 8) if d else None
        checks["barangay_snapping_coverage"] = {"status": "warning" if n < d else "complete",
                                                  "source_log": path, "scope": "population coverage only"}
    else:
        checks["barangay_snapping_coverage"] = {"status": "not_assessed"}
    if "province" in found:
        (towns, people), path = found["province"]
        towns, people = _int(towns), _int(people)
        metrics.update({"province_excluded_towns": towns, "province_excluded_population": people})
        checks["province_aggregation_completeness"] = {"status": "warning" if towns else "complete",
                                                       "source_log": path}
    else:
        checks["province_aggregation_completeness"] = {"status": "not_assessed"}
    if "ferry" in found:
        (terminals, distance), path = found["ferry"]
        metrics.update({"unmatched_offshore_ferry_terminals": _int(terminals),
                        "ferry_road_attachment_threshold_km": float(distance)})
        checks["ferry_connection_review"] = {"status": "warning" if _int(terminals) else "complete",
                                             "source_log": path,
                                             "scope": "Connection rule only; not ferry availability"}
    else:
        checks["ferry_connection_review"] = {"status": "not_assessed"}
    if "verify" in found:
        (samples, destinations, error, mismatch), path = found["verify"]
        samples, destinations, mismatch = _int(samples), _int(destinations), _int(mismatch)
        metrics.update({"verification_sources": samples, "verification_destination_sites": destinations,
                        "max_verified_distance_error_km": float(error),
                        "verification_reachability_mismatches": mismatch})
        checks["independent_shortest_path_samples"] = {
            "status": "sample_pass" if mismatch == 0 else "failed",
            "source_log": path, "tested_sources": samples,
            "scope": "Sampled numerical equivalence; not full graph verification"}
    else:
        checks["independent_shortest_path_samples"] = {"status": "not_assessed"}
    if "sites" in found:
        (selector, sites), path = found["sites"]
        metrics.update({"median_selection": selector, "representative_sites": _int(sites)})
    if "graph" in found:
        (vertices, nodes), path = found["graph"]
        metrics.update({"raw_vertices": _int(vertices), "graph_nodes": _int(nodes)})
    checks["national_component_connectivity_audit"] = {"status": "not_assessed",
        "scope": "This fresh-run sequence does not execute connectivity stage"}
    checks["real_world_ferry_service_validity"] = {"status": "not_assessed"}
    checks["legal_boundary_certification"] = {"status": "not_assessed"}
    phase_list = list(phases or [])
    return {
        "schema": "net-distance-ph/observability/v48",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "scientific_validation": "not_established",
        "workspace": str(Path(workspace).resolve()),
        "git_commit": git_commit,
        "source_releases": _provenance(workspace),
        "log_files": [str(Path(x)) for x in log_files],
        "metrics": metrics, "checks": checks,
        "stage_timings": list(stages or []),
        "phase_timings_and_resources": phase_list,
        "cautions": ["This report reflects checks actually found in log evidence; no additional network calculations were performed.",
                     "Unmeasured or unexecuted checks are not_assessed, never implicitly passed.",
                     "Sampled process-tree RSS may double-count shared pages; sampled peaks can miss short spikes."]}


def report_existing(workspace, *, write=True):
    """Analyze the existing run without executing preparation or routing."""
    workspace = Path(workspace).expanduser().resolve()
    logs = workspace / "logs"
    summary_file = logs / "fresh_run_summary.json"
    summary = json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.is_file() else {}
    matched = []
    for stage in summary.get("stages", []):
        for path in stage.get("log_files", []):
            if Path(path).is_file():
                matched.append(Path(path))
    if not matched:
        # Legacy summaries have no explicit log linkage. Use last session logs
        # and record that the association is approximate instead of hiding it.
        matched = sorted(logs.glob("run-*.log"), key=lambda p: p.name)[-3:]
    if not matched:
        raise FileNotFoundError(f"No session logs to analyze under {logs}")
    journal = logs / "fresh_run_journal.jsonl"
    phases = summary.get("phases", [])
    if journal.is_file() and not phases:
        with journal.open(encoding="utf-8") as f:
            phases = [v for line in f if line.strip() for v in [json.loads(line)]
                      if v.get("event") in ("phase_finish", "phase_failed")]
    report = build_report(workspace, matched, status=summary.get("status", "unknown"),
                          stages=summary.get("stages", []), phases=phases,
                          git_commit=summary.get("git_commit"))
    report["log_matching"] = "explicit" if any(s.get("log_files") for s in summary.get("stages", [])) else "inferred_latest_three"
    if write:
        target = logs / "observability_report.json"
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        report["report_path"] = str(target)
    return report
