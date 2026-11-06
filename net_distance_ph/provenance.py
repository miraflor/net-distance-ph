"""Source-to-output identities for independently acquired geographic inputs.

Hash the contents of every mandatory and existing optional shapefile sidecar. A
later pipeline stage refuses a source whose bytes differ from the graph build.
The paths are recorded for audit, but hashes (not timestamps) define identity.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

REQUIRED = (".shp",)
# The routing engine reads only geometry for road/ferry inputs; their DBF
# tables may legitimately be absent. Point IDs require the DBF.
OPTIONAL = (".shx", ".prj", ".cpg", ".provenance.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def shapefile_manifest(paths: Mapping[str, Path], root: Path) -> dict:
    """Record exact files used, without loading them in memory.

    Separate files can be the same length and modification time yet contain
    different coordinates. SHA-256 makes that difference observable.
    """
    entries = {}
    root = Path(root).resolve()
    for role, supplied in sorted(paths.items()):
        shp = Path(supplied).resolve()
        if shp.suffix.lower() != ".shp":
            raise ValueError(f"{role} must be a .shp file: {shp}")
        records = []
        for ext in (*REQUIRED, ".dbf", *OPTIONAL):
            file = shp.with_suffix(ext)
            if not file.is_file():
                if ext in REQUIRED or (role == "localities" and ext == ".dbf"):
                    raise FileNotFoundError(f"Missing {role} shapefile component: {file}")
                continue
            records.append({
                "extension": ext,
                "sha256": sha256_file(file),
                "bytes": file.stat().st_size,
            })
        try:
            location = str(shp.relative_to(root))
        except ValueError:
            location = str(shp)
        entries[role] = {"relative_path": location, "files": records}
    identity = {"version": 1, "inputs": entries}
    return {**identity, "fingerprint": canonical_hash(identity)}
