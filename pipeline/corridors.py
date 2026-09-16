"""Discover structurally distinct corridors in a SUMO network.

The mechanism in Chapter 3 assigns demand across corridors that are genuine
alternatives: different roads, different capacities, materially different
free-flow times. Deriving alternatives by perturbing a shortest path does
not produce that — on a dense grid it returns a near-copy of the original,
and an assignment rule with nothing to choose between cannot express any
policy at all.

This module finds corridors by construction instead. It repeatedly solves
for the fastest route between a fixed origin and destination, then heavily
penalises every edge that route used before solving again. Successive
solutions are therefore forced onto different roads, and the result is a set
of routes that are edge-disjoint by construction rather than by luck.

Each discovered corridor is characterised by the two quantities the
volume-delay function needs:

    T0  free-flow traversal time, summed over the route
    C   effective capacity, taken as the bottleneck along the route, since a
        corridor can carry no more than its narrowest segment

Because corridors are precomputed, per-vehicle assignment becomes a softmax
over K numbers rather than a shortest-path search. That is what makes ten
thousand vehicles tractable.

Usage:
    python -m pipeline.corridors --discover 3
"""

from __future__ import annotations

import argparse
import heapq
import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

NET = Path("data/net/corridor.net.xml")
OUT = Path("data/net/corridors.json")

#: Vehicles per metre of lane at jam density: roughly one per 7 m.
JAM_PER_M = 1.0 / 7.0

#: Fraction of jam density at which a corridor is considered at capacity.
#:
#: This constant decides whether the mechanism has anything to respond to.
#: BPR congestion scales as (N/C)^4, so if C is set to jam storage the ratio
#: stays near 0.2 at realistic loads, 0.2^4 is under 0.002, and every
#: corridor costs the same as when empty — the assignment rule then has no
#: congestion signal and cannot distinguish a full corridor from an empty
#: one. Capacity is therefore the load at which flow begins to break down,
#: roughly a quarter of jam storage, not the load at which the road is
#: physically full.
CAPACITY_FRACTION = 0.25

VEH_PER_M = JAM_PER_M * CAPACITY_FRACTION

#: Multiplier applied to edges already used by a discovered corridor. Large
#: enough to force the next search onto different roads, finite so that a
#: shared bottleneck can still be reused when the network leaves no choice.
REUSE_PENALTY = 40.0


@dataclass
class Corridor:
    """One discovered route, with its volume-delay parameters."""

    name: str
    edges: list[str]
    t0: float
    capacity: float
    length_m: float
    min_lanes: int
    bottleneck_edge: str
    overlap_with_primary: float

    def to_dict(self) -> dict:
        return asdict(self)


def _edge_graph(net):
    """Adjacency over edges, keyed by edge id."""
    adjacency: dict[str, list[str]] = {}
    for edge in net.getEdges():
        if not edge.allows("passenger"):
            continue
        adjacency[edge.getID()] = [
            nxt.getID()
            for nxt in edge.getOutgoing()
            if nxt.allows("passenger")
        ]
    return adjacency


def _edge_costs(net):
    """Free-flow time, length, capacity and lane count for every usable edge."""
    t0: dict[str, float] = {}
    length: dict[str, float] = {}
    capacity: dict[str, float] = {}
    lanes: dict[str, int] = {}
    for edge in net.getEdges():
        if not edge.allows("passenger"):
            continue
        eid = edge.getID()
        ln = max(edge.getLength(), 1.0)
        sp = max(edge.getSpeed(), 1.0)
        n_lanes = max(len(edge.getLanes()), 1)
        t0[eid] = ln / sp
        length[eid] = ln
        capacity[eid] = max(n_lanes * ln * VEH_PER_M, 1.0)
        lanes[eid] = n_lanes
    return t0, length, capacity, lanes


def _dijkstra(
    adjacency: dict[str, list[str]],
    cost: dict[str, float],
    penalty: dict[str, float],
    source: str,
    target: str,
) -> list[str] | None:
    """Least-cost edge path from `source` to `target` under a penalty map.

    Operates on the edge graph rather than the node graph so that penalties
    apply to roads, which is what needs to be avoided when searching for a
    disjoint alternative.
    """
    if source not in adjacency or target not in adjacency:
        return None
    dist = {source: cost.get(source, 1.0) * penalty.get(source, 1.0)}
    prev: dict[str, str] = {}
    queue: list[tuple[float, str]] = [(dist[source], source)]
    seen: set[str] = set()

    while queue:
        d, edge = heapq.heappop(queue)
        if edge in seen:
            continue
        seen.add(edge)
        if edge == target:
            break
        for nxt in adjacency.get(edge, ()):
            step = cost.get(nxt, 1.0) * penalty.get(nxt, 1.0)
            nd = d + step
            if nd < dist.get(nxt, float("inf")):
                dist[nxt] = nd
                prev[nxt] = edge
                heapq.heappush(queue, (nd, nxt))

    if target not in seen:
        return None

    path = [target]
    while path[-1] != source:
        node = prev.get(path[-1])
        if node is None:
            return None
        path.append(node)
    return list(reversed(path))


def pick_endpoints(net) -> tuple[str, str]:
    """Choose an origin and destination spanning the network.

    Selects the pair of fringe edges with the greatest separation, so that
    routes between them traverse the extract rather than terminating inside
    it. A short origin-destination pair would admit only one sensible route
    and leave the mechanism nothing to assign.
    """
    fringe = [
        e for e in net.getEdges()
        if e.allows("passenger")
        and (len(e.getIncoming()) == 0 or len(e.getOutgoing()) == 0)
    ]
    if len(fringe) < 2:
        fringe = [e for e in net.getEdges() if e.allows("passenger")]

    sources = [e for e in fringe if e.getOutgoing()]
    sinks = [e for e in fringe if e.getIncoming()]
    if not sources or not sinks:
        raise SystemExit("Network has no usable fringe edges.")

    best = None
    best_d = -1.0
    # Cap the search so very large fringe sets stay tractable.
    for s in sources[:80]:
        sx, sy = s.getShape()[0]
        for t in sinks[:80]:
            if t.getID() == s.getID():
                continue
            tx, ty = t.getShape()[-1]
            d = (sx - tx) ** 2 + (sy - ty) ** 2
            if d > best_d:
                best_d = d
                best = (s.getID(), t.getID())
    if best is None:
        raise SystemExit("Could not select endpoints.")
    return best


def discover_matrix(
    net_path: Path = NET,
    k: int = 3,
    n_origins: int = 4,
    n_destinations: int = 4,
    max_pairs: int = 16,
) -> dict:
    """Discover corridor sets across an origin-destination matrix.

    Routing every vehicle to a single destination guarantees congestion at
    that destination no matter how demand is spread beforehand: the
    corridors converge, and the queue simply forms where they meet. Under
    that structure no assignment rule can reduce congestion, which makes the
    mechanism look ineffective when the fault is in the demand model.

    Real demand has many destinations. This builds a matrix of
    origin-destination pairs, each with its own corridor set, so that
    distributing demand actually distributes where it goes — which is the
    condition under which cooperative assignment can help at all.
    """
    import sumolib

    net = sumolib.net.readNet(str(net_path))

    entries = [
        e for e in net.getEdges()
        if e.allows("passenger") and e.getOutgoing() and not e.getIncoming()
    ]
    exits = [
        e for e in net.getEdges()
        if e.allows("passenger") and e.getIncoming() and not e.getOutgoing()
    ]
    if not entries:
        entries = [e for e in net.getEdges() if e.allows("passenger") and e.getOutgoing()]
    if not exits:
        exits = [e for e in net.getEdges() if e.allows("passenger") and e.getIncoming()]

    def spread(edges, n):
        """Pick n edges spaced apart, so entry and exit points are not
        clustered in one corner of the network."""
        if len(edges) <= n:
            return edges
        pts = [(e, e.getShape()[0]) for e in edges]
        chosen = [pts[0]]
        while len(chosen) < n:
            best, best_d = None, -1.0
            for e, (x, y) in pts:
                if any(e.getID() == c[0].getID() for c in chosen):
                    continue
                d = min((x - cx) ** 2 + (y - cy) ** 2 for _, (cx, cy) in chosen)
                if d > best_d:
                    best_d, best = d, (e, (x, y))
            if best is None:
                break
            chosen.append(best)
        return [e for e, _ in chosen]

    origins = spread(entries, max(n_origins * 2, n_origins))
    dests = spread(exits, max(n_destinations * 2, n_destinations))

    # Minimum journey length.
    #
    # Selecting origins and destinations independently can pair an entry
    # with an exit a couple of hundred metres away. Over such a distance
    # there is no alternative route to find, so discovery returns the same
    # path repeatedly and the corridor set is degenerate. Requiring a
    # journey to span a meaningful fraction of the network guarantees the
    # trip is long enough for genuine alternatives to exist.
    xmin, ymin, xmax, ymax = net.getBoundary()
    diagonal = ((xmax - xmin) ** 2 + (ymax - ymin) ** 2) ** 0.5
    min_separation = 0.35 * diagonal

    #: Alternates sharing more than this fraction of edges with the primary
    #: route are variations of one road, not distinct options.
    MAX_OVERLAP = 0.6

    candidates = []
    for o in origins:
        ox, oy = o.getShape()[0]
        for d in dests:
            if o.getID() == d.getID():
                continue
            dx, dy = d.getShape()[-1]
            sep = ((ox - dx) ** 2 + (oy - dy) ** 2) ** 0.5
            if sep < min_separation:
                continue
            candidates.append((sep, o, d))

    if not candidates:
        # Fall back to the longest available journeys rather than failing.
        for o in origins:
            ox, oy = o.getShape()[0]
            for d in dests:
                if o.getID() == d.getID():
                    continue
                dx, dy = d.getShape()[-1]
                candidates.append(
                    (((ox - dx) ** 2 + (oy - dy) ** 2) ** 0.5, o, d)
                )

    # Longest journeys first: they offer the most room for alternatives.
    candidates.sort(key=lambda c: -c[0])

    groups = []
    rejected_short = 0
    rejected_overlap = 0
    for sep, o, d in candidates:
        if len(groups) >= max_pairs:
            break
        try:
            data = discover(net_path, k, o.getID(), d.getID(), _net=net)
        except SystemExit:
            continue
        if data["count"] < 2:
            rejected_short += 1
            continue
        alts = [c["overlap_with_primary"] for c in data["corridors"][1:]]
        if alts and min(alts) > MAX_OVERLAP:
            rejected_overlap += 1
            continue
        data["separation_m"] = round(sep, 1)
        groups.append(data)

    if not groups:
        raise SystemExit(
            "No origin-destination pair offered two or more corridors. "
            "The network may be poorly connected."
        )

    return {
        "pairs": len(groups),
        "rejected_no_alternative": rejected_short,
        "rejected_overlapping": rejected_overlap,
        "min_separation_m": round(min_separation, 1),
        "origins": sorted({g["origin"] for g in groups}),
        "destinations": sorted({g["destination"] for g in groups}),
        "groups": groups,
        "origin": groups[0]["origin"],
        "destination": groups[0]["destination"],
        "count": groups[0]["count"],
        "corridors": groups[0]["corridors"],
    }


def discover_many(
    net_path: Path = NET,
    k: int = 3,
    n_origins: int = 1,
) -> dict:
    """Discover corridor sets from several origins to one destination.

    A single origin edge can only admit a few vehicles per second, so demand
    beyond about a thousand vehicles never enters the network no matter how
    long the horizon: the run reports ten thousand requested and simulates a
    few hundred. Spreading departures across several entry points removes
    that ceiling, and each entry point gets its own corridor set because the
    alternatives available from one origin are not the alternatives
    available from another.
    """
    import sumolib

    net = sumolib.net.readNet(str(net_path))
    _, destination = pick_endpoints(net)

    fringe = [
        e for e in net.getEdges()
        if e.allows("passenger") and e.getOutgoing()
        and len(e.getIncoming()) == 0
    ]
    if not fringe:
        fringe = [e for e in net.getEdges() if e.allows("passenger") and e.getOutgoing()]

    dest_edge = net.getEdge(destination)
    dx, dy = dest_edge.getShape()[-1]
    # Prefer distant origins so trips traverse the network rather than
    # terminating inside it, and so entry points are spatially separated.
    ranked = sorted(
        fringe,
        key=lambda e: -((e.getShape()[0][0] - dx) ** 2 + (e.getShape()[0][1] - dy) ** 2),
    )

    groups = []
    for edge in ranked:
        if len(groups) >= n_origins:
            break
        try:
            data = discover(net_path, k, edge.getID(), destination, _net=net)
        except SystemExit:
            continue
        if data["count"] < 2:
            continue
        groups.append(data)

    if not groups:
        raise SystemExit(
            "No origin offered two or more corridors to the destination. "
            "The network may be poorly connected; inspect it with netedit."
        )

    return {
        "destination": destination,
        "origins": [g["origin"] for g in groups],
        "groups": groups,
        # Backwards compatibility with single-origin consumers.
        "origin": groups[0]["origin"],
        "count": groups[0]["count"],
        "corridors": groups[0]["corridors"],
    }


def discover(
    net_path: Path = NET,
    k: int = 3,
    origin: str | None = None,
    destination: str | None = None,
    _net=None,
) -> dict:
    """Find up to `k` structurally distinct corridors."""
    import sumolib

    net = _net if _net is not None else sumolib.net.readNet(str(net_path))
    adjacency = _edge_graph(net)
    t0, length, capacity, lanes = _edge_costs(net)

    if origin is None or destination is None:
        origin, destination = pick_endpoints(net)

    penalty: dict[str, float] = {}
    corridors: list[Corridor] = []
    primary: set[str] = set()

    for i in range(k):
        path = _dijkstra(adjacency, t0, penalty, origin, destination)
        if path is None:
            break

        edges = set(path)
        if i == 0:
            primary = set(edges)

        times = [t0[e] for e in path if e in t0]
        lengths = [length[e] for e in path if e in length]
        lanes_on_path = [lanes[e] for e in path if e in lanes]
        if not lanes_on_path:
            break

        # Corridor capacity.
        #
        # Taking the minimum storage over individual edges makes a ten-metre
        # connector stub define the capacity of a three-kilometre corridor,
        # which is physically meaningless and collapsed every corridor onto
        # the same floor value. Capacity is instead modelled as a pipe: its
        # width is the narrowest lane count along the route, its length is
        # the whole corridor, and its density is the practical occupancy.
        # That keeps capacity sensitive to the things that actually
        # constrain flow — how many lanes the tightest section has, and how
        # much road there is to hold vehicles.
        min_lanes = int(min(lanes_on_path))
        total_length = float(sum(lengths))
        corridor_capacity = max(min_lanes * total_length * VEH_PER_M, 1.0)

        # Reported separately so the true structural bottleneck stays
        # visible even though it no longer sets capacity on its own.
        per_edge = [capacity.get(e, np.inf) for e in path]
        bottleneck_idx = int(np.argmin(per_edge))
        overlap = (
            len(edges & primary) / max(1, len(edges)) if i > 0 else 1.0
        )

        corridors.append(Corridor(
            name=f"Corridor {i + 1}",
            edges=path,
            t0=round(float(sum(times)), 3),
            capacity=round(corridor_capacity, 3),
            min_lanes=min_lanes,
            length_m=round(float(sum(lengths)), 1),
            bottleneck_edge=path[bottleneck_idx],
            overlap_with_primary=round(float(overlap), 4),
        ))

        # Penalise this route's edges so the next search is pushed elsewhere.
        for e in path:
            penalty[e] = penalty.get(e, 1.0) * REUSE_PENALTY

    if not corridors:
        raise SystemExit(
            "No corridors found. The origin and destination may be "
            "disconnected; check the network with netedit."
        )

    result = {
        "origin": origin,
        "destination": destination,
        "count": len(corridors),
        "corridors": [c.to_dict() for c in corridors],
    }
    return result


def report(data: dict) -> None:
    print(f"origin      {data['origin']}")
    print(f"destination {data['destination']}")
    print(f"discovered  {data['count']} corridors\n")
    print(f"{'corridor':<12}{'T0 (s)':>9}{'capacity':>10}{'lanes':>7}"
          f"{'length (m)':>12}{'overlap':>9}{'edges':>7}")
    print("-" * 66)
    for c in data["corridors"]:
        print(f"{c['name']:<12}{c['t0']:>9.1f}{c['capacity']:>10.1f}"
              f"{c['min_lanes']:>7}{c['length_m']:>12.0f}"
              f"{c['overlap_with_primary']:>9.2f}{len(c['edges']):>7}")
    print("-" * 66)

    caps = [c["capacity"] for c in data["corridors"]]
    if max(caps) - min(caps) < 1e-6:
        print("\nWARNING: all corridors have identical capacity. Assignment "
              "can only distribute by travel time, and a symmetric corridor "
              "set has a Price of Anarchy near 1.0 — little is available to "
              "win. Check lane counts in the network.")

    if data["count"] < 2:
        print("\nWARNING: fewer than two corridors. The network offers no "
              "genuine alternative between these endpoints, so no assignment "
              "mechanism can improve on shortest-path routing here.")
        return

    overlaps = [c["overlap_with_primary"] for c in data["corridors"][1:]]
    if max(overlaps) > 0.5:
        print("\nWARNING: alternates share more than half their edges with "
              "the primary corridor. They are variations of one route rather "
              "than distinct options, and assignment will have little effect.")
    else:
        print("\nCorridors are substantially disjoint; assignment has room "
              "to distribute demand.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--net", type=Path, default=NET)
    parser.add_argument("--discover", type=int, default=3,
                        help="Number of corridors to find.")
    parser.add_argument("--origin", default=None)
    parser.add_argument("--destination", default=None)
    parser.add_argument(
        "--destinations", type=int, default=1,
        help="Number of destinations. Above 1, corridors are discovered "
             "across an origin-destination matrix so congestion is not "
             "forced onto a single exit.",
    )
    parser.add_argument(
        "--origins", type=int, default=1,
        help="Number of entry points. Raise this for demand above ~1000 "
             "vehicles; one origin edge cannot admit more.",
    )
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    if args.destinations > 1:
        data = discover_matrix(
            args.net, args.discover, args.origins, args.destinations
        )
        print(f"OD pairs kept: {data['pairs']}  "
              f"(rejected {data['rejected_no_alternative']} with no "
              f"alternative, {data['rejected_overlapping']} too overlapping)")
        print(f"minimum journey length: {data['min_separation_m']:.0f} m\n")

        # Summarise the whole matrix rather than one arbitrary pair: what
        # matters is whether the set as a whole offers real choices.
        import numpy as _np
        t0s = [[c["t0"] for c in g["corridors"]] for g in data["groups"]]
        asym = [
            (max(row) - min(row)) / max(min(row), 1e-9) for row in t0s
        ]
        overlaps = [
            c["overlap_with_primary"]
            for g in data["groups"] for c in g["corridors"][1:]
        ]
        print(f"{'metric':<34}{'mean':>10}{'min':>10}{'max':>10}")
        print("-" * 64)
        print(f"{'corridor asymmetry (T0 spread)':<34}"
              f"{_np.mean(asym):>9.1%}{_np.min(asym):>10.1%}{_np.max(asym):>10.1%}")
        print(f"{'alternate overlap':<34}"
              f"{_np.mean(overlaps):>10.2f}{_np.min(overlaps):>10.2f}"
              f"{_np.max(overlaps):>10.2f}")
        print(f"{'journey length (m)':<34}"
              f"{_np.mean([g['separation_m'] for g in data['groups']]):>10.0f}"
              f"{_np.min([g['separation_m'] for g in data['groups']]):>10.0f}"
              f"{_np.max([g['separation_m'] for g in data['groups']]):>10.0f}")
        print("-" * 64)

        if _np.mean(asym) < 0.05:
            print("\nCorridors are nearly symmetric. Selfish routing is "
                  "already close to optimal here, so expect little gain "
                  "from any assignment mechanism.")
        else:
            print(f"\nCorridors differ by {_np.mean(asym):.0%} in free-flow "
                  "time on average. There is asymmetry for the mechanism "
                  "to exploit.")

        print("\nExample pair:")
        report(data["groups"][0])
    elif args.origins > 1:
        data = discover_many(args.net, args.discover, args.origins)
        print(f"entry points: {len(data['groups'])}\n")
        for g in data["groups"]:
            report(g)
            print()
    else:
        data = discover(args.net, args.discover, args.origin, args.destination)
        report(data)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
