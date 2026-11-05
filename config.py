"""
Runtime paths, modeling assumptions, checkpoints and verification defaults.
"""

from pathlib import Path
import os

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
# Keep the historical input filenames and algorithm defaults unchanged.
# Paths are relative to this repository unless overridden by environment
# variables. No private machine paths or raw datasets are committed.
PROJECT_DIR = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PHNETDIST_DATA", PROJECT_DIR / "data")).expanduser()
WORK = Path(os.environ.get("PHNETDIST_WORK", PROJECT_DIR / "work")).expanduser()
OUT = Path(os.environ.get("PHNETDIST_OUT", PROJECT_DIR / "out")).expanduser()

ROAD_SHP = "primsectertrunk_road.shp"
FERRY_SHP = "ferry_route_shape.shp"
CENTROID_SHP = "muncity_centroids.shp"

# Attribute columns in the centroid .dbf. None = auto-detect: the ID looks for
# a field named like psgc/adm.*code/id, the label for name/mun/city.
CENTROID_ID_COL = None
CENTROID_NAME_COL = None
DBF_ENCODING = "utf-8"

# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------
# Coordinate rounding used to decide that two vertices are the same node.
# 1e-6 deg ~ 0.11 m: heals float noise from the shapefile round-trip.
# Coordinate-only noding can join grade-separated ways with coincident XY;
# preserving source OSM node identity would require a topology-aware format.
SNAP_DECIMALS = 6

# 'auto' uses pyproj (exact Karney geodesics) when installed and falls back to
# the built-in Lambert formula, which agrees with it to ~1.4 mm per km.
GEODESIC_BACKEND = "auto"

# A ferry terminal digitised offshore of the road end gets a gangway edge to
# the nearest road node within this distance. Too small strands islands; too
# large invents links across narrow straits that have no actual service.
FERRY_CONNECT_KM = 5.0

# Leave these alone for a plain distance matrix. Raise them to turn the output
# into a generalised cost that prices in the slowness and waiting time of a
# sea crossing (e.g. multiplier 3.0 and penalty 25 km for a RORO hop).
FERRY_COST_MULTIPLIER = 1.0
FERRY_FIXED_PENALTY_KM = 0.0

# Centroid attachment.
PREFER_ROAD_SNAP = True  # try road nodes first, then any node
MAX_CENTROID_SNAP_KM = 25.0  # beyond this a town is reported unsnappable
INCLUDE_SNAP_OFFSET = True  # add the centroid->network stub at both ends

# Fallback attachment (snap tier 1) for a point with no road node in range.
# False keeps the historical rule: the nearest node of any kind, which can be
# an intermediate shape point in the middle of a ferry route that merely
# passes the island. True excludes those shape points, so the fallback can
# only be a road node or a ferry polyline endpoint -- the same rule that
# FERRY_CONNECT_KM already applies to gangway edges. Changing this changes
# the distances of tier-1 towns only.
SNAP_FALLBACK_TERMINALS_ONLY = False

# --------------------------------------------------------------------------
# Reduction
# --------------------------------------------------------------------------
# Degree cap for node contraction. 1 and 2 always shrink the graph, 3 is
# neutral, 4+ trades edges for nodes. 4 is a good default for road networks;
# raise to 5-6 if you have RAM to spare and want a smaller core.
MAX_CONTRACT_DEGREE = 4
MAX_EDGE_GROWTH = 1.05

# Stage-2 observability and recovery. A heartbeat is printed and appended to
# work/reduce_progress.log while a contraction pass is busy. A crash-safe
# checkpoint is written after this many completed passes; rerunning
# ``python run.py reduce`` resumes automatically.
REDUCE_HEARTBEAT_SECONDS = 30
REDUCE_CHECKPOINT_EVERY_PASSES = 1

# --------------------------------------------------------------------------
# Reduction, part two
# --------------------------------------------------------------------------
# Relabel core nodes in reverse Cuthill-McKee order before routing. Pure cache
# locality -- it changes no distance -- and measured at 1.44x on a core graph
# of this shape. No reason to turn it off except to reproduce old node ids.
RCM_REORDER = True

# --------------------------------------------------------------------------
# Matrix
# --------------------------------------------------------------------------
N_WORKERS = 0  # 0 = cpu_count() - 1; override with --workers on the CLI
BATCH_SIZE = 8  # sources per Dijkstra call per worker
CHECKPOINT_EVERY = 8  # flush matrix + radii every N completed batches

# --- neighbour priority ---------------------------------------------------
# Sources are visited in breadth-first order over the Euclidean k-nearest-
# neighbour graph, so partial output covers a contiguous region and each
# batch is geographically tight.
KNN_K = 8
SOURCE_ORDER = "knn_bfs"  # "knn_bfs" or "input"

# Waves. Each entry is an order in the Euclidean kNN graph: 1 = the first
# neighbour ring, 2 = neighbours of neighbours, and so on. Each wave runs a
# radius-limited Dijkstra covering that neighbourhood, so near pairs resolve
# almost immediately; an unlimited wave is always appended last, so the final
# matrix is identical either way.
#
# The waves cost extra -- roughly +20% total for this default -- because a
# radius-limited run cannot be resumed into a wider one. Set WAVE_ORDERS = ()
# to pay nothing and get no early output.
WAVE_ORDERS = (1, 2)

# Network distance always exceeds straight-line distance, often by a lot in an
# archipelago. Inflating the Euclidean radius makes each wave resolve more of
# its intended neighbours, at a cost that grows roughly with the square.
WAVE_SCALE = 1.5

# Rewrite out/ after every wave, so an interrupted run still leaves a usable
# (and clearly labelled) partial matrix on disk.
WRITE_PARTIAL_EACH_WAVE = True

# --------------------------------------------------------------------------
# Verification (stage 4)
# --------------------------------------------------------------------------
# Re-runs this many sources against the *unreduced* graph and compares. This is
# the check that the contraction did not change any distance. 40 sources is
# ~65,000 pairs and takes a couple of minutes; set to 0 to skip.
VERIFY_N_SOURCES = 40
VERIFY_TOL_KM = 1e-6
VERIFY_SEED = 20260729

# Largest accepted d(i,j) - d(j,i), relative to the largest finite distance,
# measured *before* the output is made exactly symmetric. Floating-point
# summation order alone gives ~1e-15; a larger value means a defect upstream.
VERIFY_MAX_ASYMMETRY_RELATIVE = 1e-12
