import numpy as np

from net_distance_ph.graphbuild import FERRY, GANGWAY, ROAD, build_graph, connect_ferries, snap_sources
from net_distance_ph.shpio import Parts


def _line(*pts):
    xy = np.array(pts, dtype=float)
    return Parts(xy=xy, part_offset=np.array([0, len(xy)]), part_record=np.array([0]), n_records=1)


def test_snap_fallback_can_exclude_mid_route_ferry_points():
    # A road far to the west; a ferry with terminals at 120.0 and 121.0 whose
    # shape passes (120.5, 14.3). Islet P is 3 km from that mid-route point and
    # ~65 km from either terminal; islet Q is 2 km from the western terminal.
    road = _line((118.0, 14.0), (118.1, 14.0))
    ferry = _line((120.0, 14.0), (120.5, 14.3), (121.0, 14.0))
    _, _, _, _, ll, flags = build_graph([(road, ROAD), (ferry, FERRY)], verbose=False)
    towns = np.array([[120.5, 14.327], [120.015, 14.012]])

    idx, _, tier = snap_sources(towns, ll, flags, max_snap_km=25, verbose=False)
    assert tuple(ll[idx[0]]) == (120.5, 14.3) and tier[0] == 1     # historical default
    assert tuple(ll[idx[1]]) == (120.0, 14.0) and tier[1] == 1

    idx, _, tier = snap_sources(towns, ll, flags, max_snap_km=25, verbose=False,
                                fallback_terminals_only=True)
    assert tier[0] == -1                                            # no terminal in range
    assert tuple(ll[idx[1]]) == (120.0, 14.0) and tier[1] == 1


def test_only_ferry_terminals_receive_gangways():
    # Road 0--1 runs near all three ferry vertices 2--3--4. Only 2 and 4 are
    # marked terminals; vertex 3 is an intermediate shape point and must not
    # become an invented boarding point.
    node_lonlat = np.array(
        [
            [120.0000, 14.0000],
            [120.1000, 14.0000],
            [120.0002, 14.0010],
            [120.0500, 14.0010],
            [120.0998, 14.0010],
        ],
        dtype=float,
    )
    u = np.array([0, 2, 3], dtype=np.int64)
    v = np.array([1, 3, 4], dtype=np.int64)
    w = np.array([10.0, 5.0, 5.0])
    code = np.array([ROAD, FERRY, FERRY], dtype=np.int8)

    flags = np.zeros((5, 3), dtype=bool)
    flags[[0, 1], 0] = True
    flags[[2, 3, 4], 1] = True
    flags[[2, 4], 2] = True

    uu, vv, ww, cc = connect_ferries(
        u, v, w, code, node_lonlat, flags, ferry_connect_km=5.0, verbose=False
    )

    gangways = np.flatnonzero(cc == GANGWAY)
    assert gangways.size == 2
    attached_ferry_nodes = set(uu[gangways].tolist())
    assert attached_ferry_nodes == {2, 4}
    assert 3 not in attached_ferry_nodes
