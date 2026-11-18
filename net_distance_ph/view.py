"""Read-only localhost explorer for completed population-weighted OD runs.

Data are discovered from the workspace. No inference of missing routes, no
modification of source matrices or release pointers. No external JS/CDN.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import webbrowser
from urllib.parse import parse_qs, urlsplit

import numpy as np

LEVELS = ("town", "province_equivalent", "region")
CODE = re.compile(r"^[0-9]{10}$")


def _under(root: Path, rel: str) -> Path:
    """Reject absolute, escaped and symlink-escaped filenames from pointers."""
    base = root.resolve()
    path = (base / str(rel)).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Workspace release pointer escapes data root: {rel}")
    return path


def _json_file(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _file_from_pointer(data: Path, pointer: str, key: str) -> Path:
    obj = _json_file(data / pointer)
    rel = obj.get(key)
    if not isinstance(rel, str) or not rel:
        raise ValueError(f"Invalid {key} in {pointer}")
    file = _under(data, rel)
    if not file.is_file():
        raise FileNotFoundError(f"Source from {pointer} is missing: {file}")
    return file


def _code(value) -> str:
    val = str(value).strip()
    if val.endswith(".0"):
        val = val[:-2]
    if not val.isdecimal() or len(val) > 10:
        raise ValueError(f"Invalid PSGC: {val!r}")
    return val.zfill(10)


def _rows_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)


class ExplorerData:
    def __init__(self, workspace: Path, reference_units: Path | None = None):
        self.root = Path(workspace).expanduser().resolve()
        self.data = self.root / "data"
        self.outputs = self.root / "variants" / "population_weighted" / "output"
        self._runs: dict[str, dict] = {}
        self._geometry_cache: dict[str, bytes] = {}
        self._network_cache: dict[str, bytes] = {}
        self._boundary_frame = None
        self._boundary_stamp = None
        # Prepared cartography is disposable and independent of scientific outputs.
        self.view_cache = self.root / "variants" / "population_weighted" / "view_cache"
        self._lock = threading.RLock()
        self.reference_units = reference_units or (Path(__file__).resolve().parents[1] / "reference" / "population" / "default" / "units.csv")
        self._town_meta = None
        self.refresh_catalog()

    def refresh_catalog(self):
        runs = {}
        if self.outputs.is_dir():
            for directory in self.outputs.iterdir():
                if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                    continue
                available = []
                for level in LEVELS:
                    sub = directory / level
                    if (sub / "distance_matrix_km.npy").is_file() and (sub / "units.csv").is_file() and (sub / "manifest.json").is_file():
                        available.append(level)
                if available:
                    runs[directory.name] = {"id": directory.name, "levels": available}
        # The published pointer selects the preferred run without guessing from timestamps.
        latest = None
        idx = self.outputs / "latest_run.json"
        if idx.is_file():
            try:
                obj = _json_file(idx)
                target = Path(obj.get("directory", "")).resolve()
                if target.is_relative_to(self.outputs.resolve()) and target.name in runs:
                    latest = target.name
            except (OSError, ValueError, TypeError):
                pass
        with self._lock:
            self._runs = runs
            self.latest = latest or (max(runs) if runs else None)
        return self.catalog()

    def catalog(self):
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda r: r["id"], reverse=True)
            return {"runs": runs, "latest": self.latest, "workspace": str(self.root),
                    "model": "Population-weighted conditional shortest-path distance; road/ferry only",
                    "boundary_available": (self.data / "current_boundaries.json").is_file(),
                    "transport_available": (self.data / "current_transport.json").is_file()}

    def level_path(self, run: str, level: str) -> Path:
        if level not in LEVELS:
            raise ValueError(f"Unsupported level: {level}")
        with self._lock:
            info = self._runs.get(run)
        if info is None or level not in info["levels"]:
            raise FileNotFoundError(f"No completed {level} output for run {run}")
        return self.outputs / run / level

    def metadata(self, run: str, level: str):
        directory = self.level_path(run, level)
        units = []
        for i, row in enumerate(_rows_csv(directory / "units.csv")):
            if int(row["row"]) != i:
                raise ValueError(f"Nonsequential matrix rows in {directory}")
            units.append({"code": _code(row["psgc_code"]), "name": row["name"],
                          "population": int(float(row["population"])), "type": row.get("unit_type", level)})
        if len({r["code"] for r in units}) != len(units):
            raise ValueError("Duplicate PSGC codes in output")
        mat = np.load(directory / "distance_matrix_km.npy", mmap_mode="r", allow_pickle=False)
        if mat.shape != (len(units), len(units)) or mat.dtype.kind != "f":
            raise ValueError("Matrix shape/dtype does not match units.csv")
        return {"units": units, "run": run, "level": level, "n": len(units),
                "manifest": _json_file(directory / "manifest.json")}

    def binary(self, run: str, level: str, name: str) -> Path:
        if name not in {"distance_matrix_km.npy", "reachable_pair_probability.npy"}:
            raise ValueError("Unknown matrix asset")
        target = self.level_path(run, level) / name
        if not target.is_file():
            raise FileNotFoundError(str(target))
        return target

    def _town_reference(self):
        if self._town_meta is None:
            if not self.reference_units.is_file():
                raise FileNotFoundError(f"PSGC hierarchy missing: {self.reference_units}. Reinstall package with bundled reference data.")
            mapping = {}
            for row in _rows_csv(self.reference_units):
                if row["level"] in ("city", "municipality"):
                    code = _code(row["psgc_code"])
                    mapping[code] = {"region": _code(row["region_psgc"]),
                                     "province": _code(row["province_psgc"]) if row["province_psgc"] else ""}
            self._town_meta = mapping
        return self._town_meta

    @staticmethod
    def _source_stamp(source: Path) -> str:
        """Version display assets by selected files and shapefile sidecars.

        The source release's immutable identity is incorporated through the
        resolved path; size and mtime detect out-of-band file edits. This is a
        cache key, not a substitute for the provenance checks on model inputs.
        """
        paths = [source]
        if source.suffix.lower() == ".shp":
            paths += [source.with_suffix(s) for s in (".shx", ".dbf", ".prj", ".cpg")]
        attrs = []
        for part in paths:
            if part.exists():
                st = part.stat()
                attrs.append((str(part.resolve()), st.st_size, st.st_mtime_ns))
        return hashlib.sha256(json.dumps(attrs).encode()).hexdigest()[:24]

    @staticmethod
    def _compact_coordinates(coords):
        """Round display-only lon/lat geometry to about metre-level precision."""
        if len(coords) and isinstance(coords[0], (float, int)):
            return [round(float(coords[0]), 5), round(float(coords[1]), 5)]
        return [ExplorerData._compact_coordinates(c) for c in coords]

    def prepare_assets(self):
        """Precompute/reuse generalized map layers when `view` starts.

        Never modify the GeoPackage, Shapefiles or OD matrices. Failures in one
        display layer are reported, but do not stop other output selections.
        """
        latest = self.latest
        if latest is None:
            return
        levels = next((r["levels"] for r in self.catalog()["runs"] if r["id"] == latest), [])
        print("Preparing cached map layers (current boundary and transport releases)...", flush=True)
        for level in levels:
            try:
                payload = self.geometry(latest, level)
                print(f"  {level} polygons: {len(payload):,} JSON bytes ready", flush=True)
            except (ValueError, OSError, RuntimeError, ImportError) as exc:
                print(f"  {level} boundaries unavailable: {exc}", flush=True)
        for layer in ("roads", "ferries"):
            try:
                payload = self.network(layer)
                print(f"  {layer} skeleton: {len(payload):,} JSON bytes ready", flush=True)
            except (ValueError, OSError, RuntimeError, ImportError) as exc:
                print(f"  {layer} overlay unavailable: {exc}", flush=True)
        print(f"Cartographic cache: {self.view_cache}", flush=True)

    def geometry(self, run: str, level: str) -> bytes:
        if level == "province_equivalent":
            manifest = self.metadata(run, level)["manifest"]
            if manifest.get("province_crosswalk_sha256"):
                raise ValueError("This run used a custom province crosswalk; its exact mapping was not exported. Refusing inaccurate dissolved province polygons.")
        source = _file_from_pointer(self.data, "current_boundaries.json", "boundaries")
        meta = self.metadata(run, level)
        units = meta["units"]
        model_codes = {u["code"] for u in units}
        source_stamp = self._source_stamp(source)
        reference_stamp = self._source_stamp(self.reference_units) if level != "town" else ""
        identifiers = [(u["code"], u["name"]) for u in units]
        signature = hashlib.sha256(json.dumps({"v": "cartographic-v3", "level": level,
            "boundary": source_stamp, "reference": reference_stamp, "units": identifiers},
            sort_keys=True).encode()).hexdigest()[:24]
        cached_path = self.view_cache / f"boundaries-{level}-{signature}.geojson.gz"
        with self._lock:
            if signature in self._geometry_cache:
                return self._geometry_cache[signature]
        if cached_path.is_file():
            result = gzip.decompress(cached_path.read_bytes())
            with self._lock:
                self._geometry_cache[signature] = result
            return result
        try:
            import geopandas as gpd
            from shapely.geometry import mapping
            from shapely.ops import unary_union
        except ImportError as exc:
            raise RuntimeError('Map polygons require geopandas+shapely; install the geo extra') from exc
        # One in-memory GeoPackage read across town/province/region. All later
        # view sessions reuse compressed cartographic files on disk.
        with self._lock:
            if self._boundary_frame is None or self._boundary_stamp != source_stamp:
                frame = gpd.read_file(source, layer="areas")
                if "psgc_code" not in frame.columns:
                    raise ValueError("Boundary layer missing psgc_code")
                if frame.crs is None:
                    raise ValueError("Boundary source CRS missing")
                frame = frame.to_crs("EPSG:4326")
                codes = frame["psgc_code"].map(_code)
                if codes.duplicated().any():
                    raise ValueError("Duplicate boundary PSGC codes")
                frame["_view_psgc"] = codes
                self._boundary_frame = frame
                self._boundary_stamp = source_stamp
            frame = self._boundary_frame
        reference = self._town_reference() if level != "town" else {}
        grouped = {}
        for code, geom in zip(frame["_view_psgc"], frame.geometry):
            if geom is None or geom.is_empty:
                continue
            if level == "town":
                owner = code
            elif code in reference:
                owner = reference[code]["region"] if level == "region" else (reference[code]["province"] or code)
            else:
                continue
            if owner in model_codes:
                grouped.setdefault(owner, []).append(geom)
        features = []
        for u in units:
            geoms = grouped.get(u["code"], [])
            if not geoms:
                continue
            merged = unary_union(geoms) if len(geoms) > 1 else geoms[0]
            # Simplify only for display. The original GeoPackage stays intact.
            simplified = merged.simplify(0.004, preserve_topology=True)
            center = merged.representative_point()
            geom = mapping(simplified)
            features.append({"type": "Feature", "properties": {"code": u["code"],
                "name": u["name"], "center": [round(center.x, 5), round(center.y, 5)]},
                "geometry": {"type": geom["type"], "coordinates": self._compact_coordinates(geom["coordinates"])}})
        payload = {"type": "FeatureCollection", "features": features,
                   "metadata": {"level": level, "release": source.parent.name,
                   "source": "current_boundaries.json", "display_simplification_deg": 0.004,
                   "missing": len(model_codes) - len(features), "cached_display_geometry": True,
                   "warning": "Polygons are display-only, from current boundary release; historical run may use different source."}}
        result = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.view_cache.mkdir(parents=True, exist_ok=True)
        temporary = cached_path.with_name(cached_path.name + ".tmp")
        try:
            temporary.write_bytes(gzip.compress(result, compresslevel=5))
            os.replace(temporary, cached_path)
        finally:
            temporary.unlink(missing_ok=True)
        with self._lock:
            self._geometry_cache[signature] = result
        return result

    def network(self, layer: str) -> bytes:
        if layer not in {"roads", "ferries"}:
            raise ValueError("Unknown map layer")
        source = _file_from_pointer(self.data, "current_transport.json", layer)
        cache_key = hashlib.sha256((f"cartographic-v3:{layer}:{self._source_stamp(source)}").encode()).hexdigest()[:24]
        with self._lock:
            if cache_key in self._network_cache:
                return self._network_cache[cache_key]
        cache_dir = self.view_cache
        cached_path = cache_dir / f"{layer}-{cache_key}.json.gz"
        if cached_path.is_file():
            payload = gzip.decompress(cached_path.read_bytes())
            with self._lock:
                self._network_cache[cache_key] = payload
            return payload
        try:
            import shapefile
        except ImportError as exc:
            raise RuntimeError('Transport overlay requires pyshp: python -m pip install "pyshp>=2.3,<3"') from exc
        # OSM_CLASS contains original highway classification.
        classes = {"motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
        arrays = []
        counts = {}
        with shapefile.Reader(str(source)) as reader:
            fields = [field[0].upper() for field in reader.fields[1:]]
            ix = fields.index("OSM_CLASS") if "OSM_CLASS" in fields else None
            for i, rec in enumerate(reader.iterRecords()):
                cls = (str(rec[ix]).strip().lower() if ix is not None else "ferry")
                if layer == "roads" and cls not in classes:
                    continue
                shape = reader.shape(i)
                points = shape.points
                if len(points) < 2:
                    continue
                starts = list(shape.parts) + [len(points)]
                for a, b in zip(starts, starts[1:]):
                    geom = points[a:b]
                    if len(geom) < 2:
                        continue
                    # Cartographic thinning only; do not alter routing graph.
                    spaced = _thin(geom, 0.003 if layer == "roads" else 0.001)
                    arrays.append([cls, spaced, [min(p[0] for p in spaced), min(p[1] for p in spaced), max(p[0] for p in spaced), max(p[1] for p in spaced)]])
                    counts[cls] = counts.get(cls, 0) + 1
        obj = {"layer": layer, "source": str(source.name), "counts": counts, "lines": arrays,
               "note": "Cartographic skeleton from the selected transport release; not a routing-edge rendering."}
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = cached_path.with_suffix(".tmp")
        try:
            temporary.write_bytes(gzip.compress(payload, compresslevel=5))
            os.replace(temporary, cached_path)
        finally:
            temporary.unlink(missing_ok=True)
        with self._lock:
            self._network_cache[cache_key] = payload
        return payload


def _thin(points, threshold: float):
    kept = []
    last = None
    for x, y in points:
        if last is None or (x - last[0]) ** 2 + (y - last[1]) ** 2 >= threshold ** 2:
            last = (x, y)
            kept.append([round(x, 5), round(y, 5)])
    x, y = points[-1]
    last_point = [round(x, 5), round(y, 5)]
    if kept[-1] != last_point:
        kept.append(last_point)
    if len(kept) == 1:
        kept.append(last_point)
    return kept


def make_handler(data: ExplorerData):
    html = Path(__file__).with_name("view_app.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            uri = urlsplit(self.path)
            params = parse_qs(uri.query)
            run = params.get("run", [data.latest])[0]
            level = params.get("level", ["town"])[0]
            try:
                if uri.path == "/":
                    return self.send_data(html, "text/html; charset=utf-8")
                if uri.path == "/api/catalog":
                    data.refresh_catalog()
                    return self.send_json(data.catalog())
                if uri.path == "/api/metadata":
                    return self.send_json(data.metadata(run, level))
                if uri.path == "/api/geometry":
                    return self.send_data(data.geometry(run, level), "application/geo+json", compress=True)
                if uri.path == "/api/network":
                    layer = params.get("layer", ["ferries"])[0]
                    return self.send_data(data.network(layer), "application/json", compress=True)
                if uri.path in {"/api/matrix", "/api/reachability"}:
                    name = "distance_matrix_km.npy" if uri.path == "/api/matrix" else "reachable_pair_probability.npy"
                    return self.send_file(data.binary(run, level, name))
                self.send_error(404, "Unknown endpoint")
            except (ValueError, OSError, RuntimeError, KeyError) as exc:
                self.send_json({"error": str(exc)}, code=400)

        def send_json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return self.send_data(body, "application/json", status=code)

        def send_file(self, path):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with path.open("rb") as fh:
                while True:
                    part = fh.read(1024 * 1024)
                    if not part:
                        break
                    self.wfile.write(part)

        def send_data(self, body: bytes, content_type: str, status=200, compress=False):
            if compress and "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 4096:
                body = gzip.compress(body, compresslevel=5)
                zipped = True
            else:
                zipped = False
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if zipped:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            # Only HTTP summaries; no personal paths or matrix values.
            return

    return Handler


def run_view(workspace: Path | None = None, *, port: int = 0, open_browser: bool = True) -> int:
    if workspace is None:
        workspace = Path.home() / "Documents" / "net-distance-ph"
    data = ExplorerData(workspace)
    if not data.catalog()["runs"]:
        raise FileNotFoundError(f"No completed population-weighted output runs in {data.outputs}")
    if not 0 <= port <= 65535:
        raise ValueError("--port must be in [0, 65535]")
    data.prepare_assets()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(data))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Net Distance PH Explorer: {url}", flush=True)
    print(f"Completed runs: {len(data.catalog()['runs'])}; latest {data.latest}", flush=True)
    print("Read-only local viewer. Press Ctrl+C to stop.", flush=True)
    try:
        if open_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Explorer stopped.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()
    raise SystemExit(run_view(args.workspace, port=args.port, open_browser=not args.no_open))
