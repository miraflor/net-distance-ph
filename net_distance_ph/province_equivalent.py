"""Nationally exhaustive province-equivalent OD-zone hierarchy.

A standard administrative aggregation of the existing town matrix, not an
independent routing model. Each City/Municipality maps to exactly one unit:
its published provincial parent, or itself when it has no provincial parent.

PSGC tables do not distinguish HUC from ICC by the normalized columns alone;
we deliberately call those rows 'nonprovincial_city', not a guessed legal type.
Likewise Pateros/SGA municipalities are nonprovincial_municipality, not cities.
"""
from __future__ import annotations

from collections import Counter
import numpy as np


def build_province_equivalents(roster, towns, crosswalk=None):
    """Return (units, town-to-unit index, summary), with zero omitted towns.

    `roster` is the validated load_plain_dataset mapping; `towns` follows the
    OD matrix order. `crosswalk` is an explicitly user-approved assignment to
    an already registered province, never a guessed geopolitical relationship.
    """
    crosswalk = dict(crosswalk or {})
    provinces = {c: r for c, r in roster.items() if r.get("level") == "province"}
    expected_towns = {c for c, r in roster.items() if r.get("level") in {"city", "municipality"}}
    if not towns or len(set(towns)) != len(towns) or set(towns) != expected_towns:
        raise ValueError("Province equivalents require each official town exactly once")
    if set(crosswalk) - expected_towns:
        raise ValueError("Province crosswalk references a non-town code")

    unit_by_code = {
        c: {"code": c, "name": r["name"], "unit_type": "province"}
        for c, r in provinces.items()
    }
    membership = {}
    for town in towns:
        item = roster[town]
        parent = crosswalk.get(town) or item.get("province_psgc", "")
        if parent:
            if parent not in provinces:
                raise ValueError(f"Town {town} has unrecognized province parent {parent}")
            if item.get("province_psgc") and item["province_psgc"] != parent:
                raise ValueError(f"Crosswalk would override the published parent of town {town}")
            membership[town] = parent
        else:
            typ = item.get("level")
            if typ not in {"city", "municipality"}:
                raise ValueError(f"Expected City/Municipality, got {typ!r} for {town}")
            unit_by_code[town] = {"code": town, "name": item["name"],
                                  "unit_type": "nonprovincial_city" if typ == "city"
                                  else "nonprovincial_municipality"}
            membership[town] = town

    codes = sorted(unit_by_code)
    to_index = {code: i for i, code in enumerate(codes)}
    mapping = np.array([to_index[membership[t]] for t in towns], dtype=np.int64)
    if mapping.shape != (len(towns),) or np.any(mapping < 0):
        raise AssertionError("Every town must have exactly one province equivalent")
    units = [unit_by_code[c] for c in codes]
    counts = dict(sorted(Counter(u["unit_type"] for u in units).items()))
    return units, mapping, {
        "schema": "net-distance-ph/province-equivalent-definition/v1",
        "n_towns": len(towns), "n_equivalent_units": len(units),
        "unit_types": counts, "towns_without_parent": sum(not roster[t].get("province_psgc") for t in towns),
        "explicit_crosswalk_assignments": len(crosswalk),
        "interpretation": "Registered provinces and independently represented cities/municipalities are peer OD units; this does not assert each nonprovincial city is HUC versus ICC",
        "coverage": "Each town belongs to exactly one output unit; official barangay population totals are retained",
    }
