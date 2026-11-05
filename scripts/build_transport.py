#!/usr/bin/env python3
"""Prepare standalone, versioned road/ferry shapefiles from Philippine OSM.

Usage:
    python scripts/build_transport.py --pbf data/sources/philippines.osm.pbf --output data
    python scripts/build_transport.py --download --output data

For full-country PBF files install ``pip install -e '.[transport]'`` (pyosmium
and PyShp). An uncompressed .osm XML file is supported for *small* fixtures.
This builder does not perform route verification or imply ferry availability.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlparse
import uuid
import xml.etree.ElementTree as ET

from phnetdist.provenance import sha256_file

GEOFABRIK_URL = "https://download.geofabrik.de/asia/philippines-latest.osm.pbf"
ROAD_CLASSES = ("trunk", "primary", "secondary", "tertiary")
# Philippine OSM convention tags tolled expressways highway=motorway. They are
# reachable only through their ramps, so the opt-in adds both classes.
MOTORWAY_CLASSES = ("motorway", "motorway_link")
# Roles that mark the route line itself in a type=route relation. Other roles
# (platform, stop, ...) mark terminals and stopping places, not the crossing.
ROUTE_ROLES = frozenset({"", "forward", "backward"})
# Tags that mark a facility (terminal building, pier, platform), not a route line.
FACILITY_KEYS = frozenset({"public_transport", "amenity", "man_made", "building"})
FERRY_FILTER = (
    "ways tagged route=ferry; plus member ways of type=route + route=ferry relations "
    "that have role '', forward or backward, no highway tag, no facility tag "
    "(public_transport, amenity, man_made, building), are not area=yes and are not closed rings"
)
WGS84_PRJ = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
             'SPHEROID["WGS_1984",6378137,298.257223563]],'
             'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]\n')


def _tags(items):
    """Unify OSM tag containers and XML tag dictionaries."""
    if isinstance(items, dict):
        return items
    return {str(item.k): str(item.v) for item in items}


def _is_ferry_relation(tags: dict) -> bool:
    return tags.get("type") == "route" and tags.get("route") == "ferry"


def way_kind(tags: dict, osm_id: int, ferry_members: set[int], road_classes: set[str],
             closed: bool = False):
    """Classify one OSM way as a road, a ferry line, or neither.

    A way tagged ``route=ferry`` is a ferry line, whatever else it carries.
    A way that is only a *member* of a ferry route relation counts as a ferry
    line only if it has no highway tag, no facility tag, is not an area and
    is not a closed ring. Route relations also list approach roads, terminal
    platforms and pier outlines; treating those as sea crossings turned
    platform polygons into ferry "routes" and moved approach roads out of the
    road layer. A highway-tagged member keeps its road class (or is dropped
    if that class is not selected).
    """
    if tags.get("route") == "ferry":
        return "ferries", "ferry"
    highway = tags.get("highway", "")
    if highway in road_classes:
        return "roads", highway
    if (osm_id in ferry_members and not highway and not closed
            and tags.get("area") != "yes" and not (FACILITY_KEYS & tags.keys())):
        return "ferries", "ferry"
    return None


def _is_closed(refs) -> bool:
    return len(refs) > 2 and refs[0] == refs[-1]


def _clean_coords(coords):
    cleaned = []
    for raw_lon, raw_lat in coords:
        lon, lat = float(raw_lon), float(raw_lat)
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError(f"Invalid longitude/latitude on selected OSM way: {lon}, {lat}")
        xy = [lon, lat]
        if not cleaned or xy != cleaned[-1]:
            cleaned.append(xy)
    return cleaned


def _xml_ways(path: Path, road_classes: set[str]):
    """Small-fixture fallback for ordinary XML; full-country processing uses PBF."""
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("OSM XML exceeds 100 MiB; use a PBF with the transport extra instead")
    root = ET.parse(path).getroot()
    nodes = {}
    for element in root.findall("node"):
        nodes[element.attrib["id"]] = (float(element.attrib["lon"]), float(element.attrib["lat"]))
    members = set()
    for relation in root.findall("relation"):
        tags = {t.attrib["k"]: t.attrib["v"] for t in relation.findall("tag")}
        if _is_ferry_relation(tags):
            members.update(int(m.attrib["ref"]) for m in relation.findall("member")
                           if m.attrib.get("type") == "way"
                           and m.attrib.get("role", "") in ROUTE_ROLES)
    for way in root.findall("way"):
        osm_id = int(way.attrib["id"])
        tags = {t.attrib["k"]: t.attrib["v"] for t in way.findall("tag")}
        refs = [n.attrib["ref"] for n in way.findall("nd")]
        classification = way_kind(tags, osm_id, members, road_classes, closed=_is_closed(refs))
        if classification is None:
            continue
        if any(ref not in nodes for ref in refs):
            raise ValueError(f"OSM way {osm_id} references nodes missing from XML")
        yield osm_id, classification, [nodes[ref] for ref in refs]


def _pbf_ways(path: Path, road_classes: set[str]):
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError("PBF extraction requires pyosmium: pip install -e '.[transport]'") from exc
    # pyosmium raises InvalidLocationError (derived directly from Exception,
    # not ValueError) when a way references a node that has no location.
    location_errors = (getattr(osmium, "InvalidLocationError", ValueError),
                       ValueError, RuntimeError, AttributeError)

    class Relations(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.members: set[int] = set()
            self.n_relations = 0
            self.ignored_roles = Counter()

        def relation(self, relation):
            if _is_ferry_relation(_tags(relation.tags)):
                self.n_relations += 1
                for m in relation.members:
                    if str(m.type) not in {"w", "way"}:
                        continue
                    role = str(getattr(m, "role", ""))
                    if role in ROUTE_ROLES:
                        self.members.add(int(m.ref))
                    else:
                        self.ignored_roles[role] += 1

    relation_scan = Relations()
    relation_scan.apply_file(str(path), locations=False)
    ignored = ", ".join(f"{k}: {v:,}" for k, v in sorted(relation_scan.ignored_roles.items())) or "none"
    print(f"  ferry relations: {relation_scan.n_relations:,}; "
          f"route member ways: {len(relation_scan.members):,}; "
          f"members ignored by role: {ignored}", flush=True)

    class Ways(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.consumer = None
            self.missing_locations = []

        def way(self, way):
            refs = [int(node.ref) for node in way.nodes]
            classification = way_kind(_tags(way.tags), int(way.id), relation_scan.members,
                                      road_classes, closed=_is_closed(refs))
            if classification is None:
                return
            try:
                xy = [(float(node.location.lon), float(node.location.lat)) for node in way.nodes]
            except location_errors:
                self.missing_locations.append(int(way.id))
                return
            self.consumer(int(way.id), classification, xy)

    def produce(consumer):
        handler = Ways()
        handler.consumer = consumer
        # flex_mem is pyosmium's location index; its use can consume substantial
        # RAM for national extracts. It does not acquire data from other tools.
        handler.apply_file(str(path), locations=True, idx="flex_mem")
        if handler.missing_locations:
            raise ValueError(f"{len(handler.missing_locations)} selected OSM ways lack "
                             f"node coordinates (examples: {handler.missing_locations[:8]})")

    return produce


def _sidecars(path: Path):
    path.with_suffix(".prj").write_text(WGS84_PRJ, encoding="utf-8")
    path.with_suffix(".cpg").write_text("UTF-8\n", encoding="ascii")


def _extract(source: Path, stage: Path, classes: set[str], allow_no_ferries=False) -> dict:
    try:
        import shapefile
    except ImportError as exc:
        raise RuntimeError("Install PyShp: pip install -e '.[transport]'") from exc
    road = stage / "primsectertrunk_road.shp"
    ferry = stage / "ferry_route_shape.shp"
    counts = Counter()
    rejected = Counter()
    with shapefile.Writer(str(road), shapeType=shapefile.POLYLINE, encoding="utf-8") as roads, \
            shapefile.Writer(str(ferry), shapeType=shapefile.POLYLINE, encoding="utf-8") as ferries:
        for writer in (roads, ferries):
            writer.field("OSM_ID", "C", size=24)
            writer.field("OSM_CLASS", "C", size=32)

        def write(osm_id, classification, coords):
            kind, osm_class = classification
            coordinates = _clean_coords(coords)
            if len(coordinates) < 2:
                rejected[kind] += 1
                return
            writer = roads if kind == "roads" else ferries
            writer.line([coordinates])
            writer.record(str(osm_id), osm_class)
            counts[kind] += 1
            if counts[kind] % 100000 == 0:
                print(f"  {kind}: {counts[kind]:,} OSM ways", flush=True)

        if source.suffix.lower() in (".osm", ".xml"):
            for osm_id, classification, coords in _xml_ways(source, classes):
                write(osm_id, classification, coords)
        else:
            produce = _pbf_ways(source, classes)
            produce(write)

    _sidecars(road)
    _sidecars(ferry)
    if counts["roads"] == 0:
        raise ValueError("No selected road ways were found; refusing an empty road network")
    if counts["ferries"] == 0 and not allow_no_ferries:
        raise ValueError("No ferry ways were found; check relation handling or use --allow-no-ferries")
    return {"roads": counts["roads"], "ferries": counts["ferries"],
            "degenerate_road_ways": rejected["roads"],
            "degenerate_ferry_ways": rejected["ferries"]}


def _download(url: str, destination: Path):
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("Download requires requests: pip install -e '.[transport]'") from exc
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
            "download.geofabrik.de", "download-openstreetmap.fr"}:
        raise ValueError("--source-url must use an approved HTTPS OSM extract host")
    destination.parent.mkdir(parents=True, exist_ok=True)
    acquisition = destination.with_suffix(destination.suffix + ".acquisition.json")
    if destination.exists():
        try:
            prior = json.loads(acquisition.read_text(encoding="utf-8"))
            if not isinstance(prior, dict):
                prior = {}
        except (OSError, ValueError):
            prior = {}
        known_url = prior.get("url")
        if known_url and known_url != url:
            raise ValueError(
                f"Cached OSM snapshot {destination} was downloaded from {known_url}, "
                f"not the requested {url}. Use --pbf for the existing snapshot, "
                "or archive the old cache before acquiring a different source."
            )
        print(f"  using existing downloaded snapshot {destination}")
        if not known_url:
            print("  WARNING: no acquisition metadata for cached PBF; "
                  "the original download URL cannot be verified", flush=True)
        return {"url": known_url, "requested_url": url, "cached": True,
                "acquisition_url_recorded": bool(known_url),
                "response_headers": prior.get("response_headers")}
    partial = destination.with_suffix(destination.suffix + ".part")
    with requests.get(url, stream=True, timeout=(30, 900),
                      headers={"User-Agent": "phnetdist/0.2 (research)"}) as response:
        response.raise_for_status()
        if urlparse(response.url).scheme != "https" or \
                urlparse(response.url).hostname not in {
                    "download.geofabrik.de", "download-openstreetmap.fr"}:
            raise ValueError("OSM download redirected outside the approved source hosts")
        headers = {k: response.headers.get(k) for k in ("ETag", "Last-Modified", "Content-Length")}
        try:
            with partial.open("wb") as stream:
                for block in response.iter_content(8 * 1024 * 1024):
                    if block:
                        stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    details = {"url": url, "resolved_url": response.url,
               "downloaded_utc": datetime.now(timezone.utc).isoformat(),
               "response_headers": headers}
    _atomic_json(acquisition, details)
    return {**details, "cached": False, "acquisition_url_recorded": True}


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".pointer-", suffix=".tmp", delete=False) as temp:
        tmp_name = temp.name
        json.dump(value, temp, ensure_ascii=False, indent=2)
        temp.write("\n")
        temp.flush()
        os.fsync(temp.fileno())
    try:
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def build(args):
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    pointer = output / "current_transport.json"
    if pointer.exists() and not args.replace:
        raise FileExistsError(f"{pointer} already exists; use --replace to select a new release")
    if args.download:
        if args.pbf is not None:
            raise ValueError("Use either --download or --pbf, not both")
        source = output / "sources" / "philippines-latest.osm.pbf"
        origin = _download(args.source_url, source)
    else:
        if args.pbf is None:
            raise ValueError("Provide --pbf PATH or explicitly use --download")
        source = args.pbf.expanduser().resolve()
        origin = {"local_file": True}
    if not source.is_file() or not source.stat().st_size:
        raise FileNotFoundError(f"OSM input missing/empty: {source}")
    if source.suffix.lower() not in (".pbf", ".osm", ".xml"):
        raise ValueError("Input must be OSM .pbf or small .osm/.xml")
    source_hash = sha256_file(source)
    if args.expected_sha256 and source_hash != args.expected_sha256.lower():
        raise ValueError(f"OSM SHA-256 mismatch: got {source_hash}; expected {args.expected_sha256}")
    classes = set(ROAD_CLASSES)
    if args.include_links:
        classes.update(f"{name}_link" for name in ROAD_CLASSES)
    if getattr(args, "include_motorways", False):
        classes.update(MOTORWAY_CLASSES)
    releases = output / "transport_releases"
    releases.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=releases) as temp:
        staging = Path(temp)
        counts = _extract(source, staging, classes, allow_no_ferries=args.allow_no_ferries)
        manifest = {
            "schema": 1, "source_kind": "OpenStreetMap PBF" if source.suffix.lower() == ".pbf" else "OpenStreetMap XML",
            "source": str(source), "source_sha256": source_hash, "source_acquisition": origin,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "road_filter": sorted(classes), "ferry_filter": FERRY_FILTER,
            "extraction_counts": counts, "crs": "EPSG:4326", "distance_measure": "geometry lengths; not travel time",
            "license_notice": "OpenStreetMap contributors (ODbL); comply with OSM attribution and database terms",
        }
        for filename in ("primsectertrunk_road.shp", "ferry_route_shape.shp"):
            path = staging / filename
            sidecars = {extension: sha256_file(path.with_suffix(extension))
                        for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg")}
            manifest[filename] = sidecars
            is_road = filename.startswith("prim")
            path.with_suffix(".provenance.json").write_text(
                json.dumps({"source_sha256": source_hash,
                            "filter": manifest["road_filter"] if is_road else FERRY_FILTER,
                            "geometry_count": counts["roads" if is_road else "ferries"]},
                           indent=2) + "\n", encoding="utf-8")
        (staging / "transport_provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        release_id = uuid.uuid4().hex[:16]
        destination = releases / release_id
        os.replace(staging, destination)
    base = Path("transport_releases") / release_id
    release = {"schema": 1, "release_id": release_id,
               "roads": (base / "primsectertrunk_road.shp").as_posix(),
               "ferries": (base / "ferry_route_shape.shp").as_posix(),
               "provenance": (base / "transport_provenance.json").as_posix(),
               "source_sha256": source_hash}
    _atomic_json(pointer, release)
    print(f"Published transport release: {destination}")
    print(f"Selected by: {pointer}")
    print(f"Extracted {counts['roads']:,} road ways and {counts['ferries']:,} ferry ways")
    return release


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sources = p.add_mutually_exclusive_group(required=True)
    sources.add_argument("--pbf", type=Path, help="Local national .osm.pbf snapshot (or small .osm XML test)")
    sources.add_argument("--download", action="store_true", help="Explicitly download the Philippines OSM extract")
    p.add_argument("--source-url", default=GEOFABRIK_URL, help="HTTPS URL for --download")
    p.add_argument("--expected-sha256", help="Optional expected upstream OSM SHA-256 (recommended for exact replication)")
    p.add_argument("--output", type=Path, default=Path("data"), help="Output data root (not a source repository)")
    p.add_argument("--include-links", action="store_true", help="Add trunk/primary/secondary/tertiary _link roads")
    p.add_argument("--include-motorways", action="store_true",
                   help="Add highway=motorway and motorway_link (tolled expressways in Philippine OSM); "
                        "off by default to match the historical four-class network")
    p.add_argument("--allow-no-ferries", action="store_true", help="Allow a terrestrial-only test extract")
    p.add_argument("--replace", action="store_true", help="Select a new release without deleting older ones")
    return p.parse_args(argv)


def main(argv=None):
    try:
        build(arguments(argv))
    except (ValueError, FileNotFoundError, FileExistsError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
