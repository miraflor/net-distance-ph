"""Stable output reporting and exact-hash reuse of last completed geometry.

This module does not download sources or modify computed routing state.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


_DATED_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d{3})?$")


def valid_output_run_name(name: str, routing_hash: str) -> bool:
    """Accept both v56 content-hash folders and v57 timestamp folders."""
    return name == routing_hash[:16] or bool(_DATED_DIR.fullmatch(name))


def create_timestamped_run_directory(output_root: Path, *, now: datetime | None = None) -> Path:
    """Create a unique local-time output folder, without touching routing caches."""
    stamp = (now if now is not None else datetime.now().astimezone()).strftime('%Y-%m-%d_%H-%M-%S')
    root = Path(output_root)
    for attempt in range(1000):
        name = stamp if attempt == 0 else f'{stamp}-{attempt:03d}'
        dest = root / name
        try:
            dest.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return dest.resolve()
    raise FileExistsError(f'No free timestamped output folder under {root}')


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _matching_geometry(path: Path, expected: dict) -> bool:
    if not expected or not isinstance(expected, dict):
        return False
    for ext, sha in expected.items():
        if not isinstance(ext, str) or not ext.startswith(".") or not isinstance(sha, str) or len(sha) != 64:
            return False
        candidate = path.with_suffix(ext)
        if not candidate.is_file() or _sha256(candidate) != sha:
            return False
    return True


def previous_geometry(repo: Path, workspace: Path, home: Path) -> Path | None:
    """Reuse the latest published routing run's EXACT cached polygon points.

    No older/different run or unchecked geometry is substituted. A previously
    published run pointing at unavailable geometry produces a clear error.
    No latest-run pointer means the caller may use its historical fresh-run policy.
    """
    latest_file = home / "output" / "latest_run.json"
    if not latest_file.is_file():
        return None
    latest = _safe_read_json(latest_file)
    run_hash = latest.get("routing_hash")
    if not isinstance(run_hash, str) or len(run_hash) != 64:
        raise ValueError("Latest population run has an invalid routing_hash")
    output_root = (home / "output").resolve()
    run = Path(latest.get("directory", "")).expanduser().resolve()
    if run.parent != output_root or not valid_output_run_name(run.name, run_hash):
        raise ValueError("Latest population run directory and routing hash disagree")
    manifest = None
    for level in ("town", "province_equivalent", "region", "province"):
        path = run / level / "manifest.json"
        if not path.is_file():
            continue
        candidate = _safe_read_json(path)
        if candidate.get("routing_hash") != run_hash:
            raise ValueError(f"Latest {level} matrix manifest does not match routing hash")
        if manifest is not None and candidate.get("core_cache_key") != manifest.get("core_cache_key"):
            raise ValueError("Latest matrix level manifests disagree on the barangay core")
        manifest = candidate
    if manifest is None:
        raise ValueError("Latest population run has no valid matrix manifest")
    core_key = manifest.get("core_cache_key")
    if not isinstance(core_key, str) or len(core_key) != 64:
        raise ValueError("Latest population run has an invalid barangay core key")
    meta_path = home / "cores" / core_key[:16] / "metadata.json"
    meta = _safe_read_json(meta_path)
    if meta.get("cache_key") != core_key:
        raise ValueError("Latest population core metadata has a different fingerprint")
    expected = meta.get("points_file_sha256")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("Latest population core has no recorded geometry hashes")
    geometry_folder = home / "geometry"
    candidates = sorted(geometry_folder.glob("barangay_points_*.csv"))
    candidates.append(repo / "reference" / "population" / "default" / "barangay_points.csv")
    matched = [p for p in candidates if _matching_geometry(p, expected)]
    if not matched:
        raise FileNotFoundError(
            "Cached geometry used by the latest completed run is missing or changed. "
            "Pass --points PATH to the exact source, or explicitly select "
            "--geometry-source faeldon/ocha/auto. No geometry was substituted."
        )
    print(f"GEOMETRY REUSED FROM LAST RUN (SHA-256 verified): {matched[0].resolve()}", flush=True)
    return matched[0]


def output_manifest(paths: dict[str, Path]) -> dict[str, dict[str, str]]:
    """One machine-readable location index for exactly the requested levels."""
    fields = {
        "distance_csv": "distance_matrix_km.csv",
        "distance_npy": "distance_matrix_km.npy",
        "reachability_npy": "reachable_pair_probability.npy",
        "unit_ids_csv": "units.csv",
        "level_manifest_json": "manifest.json",
    }
    result: dict[str, dict[str, str]] = {}
    for level in ("town", "province_equivalent", "region"):
        if level not in paths:
            continue
        directory = Path(paths[level]).resolve()
        absent = [name for name in fields.values() if not (directory / name).is_file()]
        if absent:
            raise FileNotFoundError(f"Cannot report incomplete {level} output at {directory}: {absent}")
        result[level] = {"directory": str(directory),
                         **{key: str(directory / file) for key, file in fields.items()}}
    if set(result) != set(paths):
        raise ValueError("Unsupported output level in location manifest")
    return result


def print_output_locations(locations: dict[str, dict[str, str]], index_path: Path) -> None:
    """Make every output discoverable from the CLI, without relying on log search."""
    print("", flush=True)
    print("=== POPULATION-WEIGHTED MATRIX OUTPUTS ===", flush=True)
    for level in ("town", "province_equivalent", "region"):
        if level not in locations:
            continue
        entry = locations[level]
        print(f"OUTPUT [{level.upper()}]: {entry['directory']}", flush=True)
        print(f"  DISTANCE CSV: {entry['distance_csv']}", flush=True)
        print(f"  DISTANCE NPY: {entry['distance_npy']}", flush=True)
        print(f"  REACHABILITY: {entry['reachability_npy']}", flush=True)
        print(f"  UNITS / ROW IDS: {entry['unit_ids_csv']}", flush=True)
    print(f"LATEST OUTPUT INDEX: {Path(index_path).resolve()}", flush=True)
    print("=== END MATRIX OUTPUTS ===", flush=True)
