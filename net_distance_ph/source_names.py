"""Source geometry filenames are independent of highway class and locality level.

The keys match the selected-release JSON schema. Always resolve a selected
release through its pointer; NEVER infer a release from filenames alone.
"""
from __future__ import annotations

# Canonical names for all *future* source releases. A locality origin can be
# a geometric centroid or another policy-defined representative point.
CANONICAL_SOURCE_NAMES: dict[str, str] = {
    "roads": "roads.shp",
    "ferries": "ferry_routes.shp",
    "centroids": "locality_origins.shp",
}

# Immutable older releases and standalone legacy inputs remain supported.
LEGACY_SOURCE_NAMES: dict[str, str] = {
    "roads": "primsectertrunk_road.shp",
    "ferries": "ferry_route_shape.shp",
    "centroids": "muncity_centroids.shp",
}


def canonical_name(role: str) -> str:
    """Canonical filename for the JSON role in a newly built release."""
    return CANONICAL_SOURCE_NAMES[role]


def accepted_source_names(role: str) -> tuple[str, str]:
    """Exact allowlist; unknown names are not silently accepted."""
    return (CANONICAL_SOURCE_NAMES[role], LEGACY_SOURCE_NAMES[role])
