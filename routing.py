"""Who goes first: OR-Tools vehicle routing for rescue teams.

Built on our own OR-Tools vehicle-routing model: RoutingIndexManager / RoutingModel, distance arc
cost, a Time dimension with service time per stop and a shift horizon, PATH_CHEAPEST_ARC +
GUIDED_LOCAL_SEARCH.

What changes for rescue:
- vehicles = rescue teams T1..T3, each starting where it is now (open routes, no return depot)
- service time = time to search a building, by damage grade
- objective = distance + priority-weighted ARRIVAL TIME (soft upper bound 0 on each building's
  arrival, cost = weight x minutes): buildings more likely to hold survivors are reached first.
- buildings with a confirmed or possible survivor get the largest weight.
Distances are straight-line (haversine). There is no road network. Runs locally on the GB10.
"""
import math

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

SPEED_M_PER_MIN = 250          # ~15 km/h through a damaged city (assumption, shown on screen)
SERVICE_MIN = {3: 90, 2: 45, 1: 20, 0: 15}   # minutes to search a building, by Copernicus grade
HORIZON_MIN = 24 * 60
SOLVE_SECONDS = 1              # short solve: rescue re-plans every 30 s


def haversine_m(a, b):
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def plan(teams, sites):
    """teams: [{"id", "lat", "lon"}]; sites: [{"id", "lat", "lon", "grade", "weight"}].
    Returns {team_id: [site ids in visiting order]}, plus eta minutes per site."""
    if not sites or not teams:
        return {t["id"]: [] for t in teams}, {}
    nt = len(teams)
    # nodes: 0..nt-1 team starts, nt = dummy end (free), nt+1.. sites
    pts = [(t["lat"], t["lon"]) for t in teams] + [None] + [(s["lat"], s["lon"]) for s in sites]
    n = len(pts)
    end = nt

    def dist(i, j):
        if i == end or j == end:
            return 0
        return int(haversine_m(pts[i], pts[j]))

    service = [0] * (nt + 1) + [SERVICE_MIN.get(s["grade"], 30) for s in sites]
    manager = pywrapcp.RoutingIndexManager(n, nt, list(range(nt)), [end] * nt)
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(fi, ti):
        return dist(manager.IndexToNode(fi), manager.IndexToNode(ti))
    routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(distance_cb))

    def time_cb(fi, ti):
        f, t = manager.IndexToNode(fi), manager.IndexToNode(ti)
        return int(dist(f, t) / SPEED_M_PER_MIN) + service[f]
    routing.AddDimension(routing.RegisterTransitCallback(time_cb), 0, HORIZON_MIN, True, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    for k, s in enumerate(sites):
        idx = manager.NodeToIndex(nt + 1 + k)
        # Weighted arrival time: every minute of delay at this building costs `weight`.
        time_dim.SetCumulVarSoftUpperBound(idx, 0, max(1, int(s["weight"])))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = SOLVE_SECONDS
    sol = routing.SolveWithParameters(params)
    if not sol:
        return greedy(teams, sites)

    routes, eta = {}, {}
    for v, t in enumerate(teams):
        idx, order = routing.Start(v), []
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node > nt:
                sid = sites[node - nt - 1]["id"]
                order.append(sid)
                eta[sid] = sol.Value(time_dim.CumulVar(idx))
            idx = sol.Value(routing.NextVar(idx))
        routes[t["id"]] = order
    return routes, eta


def greedy(teams, sites):
    """Fallback if the solver fails: highest weight first, nearest free team."""
    routes = {t["id"]: [] for t in teams}
    pos = {t["id"]: (t["lat"], t["lon"]) for t in teams}
    for s in sorted(sites, key=lambda s: -s["weight"]):
        tid = min(pos, key=lambda k: haversine_m(pos[k], (s["lat"], s["lon"])))
        routes[tid].append(s["id"])
        pos[tid] = (s["lat"], s["lon"])
    return routes, {}
