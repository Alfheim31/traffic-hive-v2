"""Generate demand and run headless SUMO scenarios for Traffic Hive.

Each scenario differs only in the *rule* applied, never in the network,
demand, or seed. That isolation is what makes the comparison defensible:
any difference in the output metrics is attributable to the rule alone.

Scenarios
---------
fixed       static signal timing, shortest-path routing (naive baseline)
actuated    gap-based actuated signals, shortest-path routing
ue          actuated signals, greedy per-vehicle rerouting (user equilibrium,
            i.e. what commercial navigation apps do)
hive        actuated signals, HPDM softmax route assignment

Usage:
    python -m pipeline.run_scenarios --scenario hive --vehicles 1000
    python -m pipeline.run_scenarios --all --vehicles 1000
"""

from __future__ import annotations

import argparse
import math
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

NET = Path("data/net/corridor.net.xml")
OUT = Path("data/out")


@dataclass(frozen=True)
class Scenario:
    """A single experimental condition."""

    name: str
    tls_type: str  # "static" | "actuated"
    routing: str  # "shortest" | "ue" | "hpdm"
    label: str
    eta: float = 0.03  # HPDM softmax sharpness; see findings, eta=0.03
    reroute_period: int = 30  # seconds between routing decisions
    # "travel_time" reproduces Stochastic User Equilibrium; "marginal" is the
    # System Optimum rule. See RoutingController._route_cost for the
    # derivation of why these differ by a factor of (n+1) on the BPR term.
    cost_basis: str = "travel_time"
    # Fairness tolerance. When set, candidate routes costing more than
    # (1 + epsilon) times the cheapest candidate are discarded before the
    # softmax draw. None disables the constraint.
    epsilon: float | None = None


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("fixed", "static", "shortest", "Fixed-time"),
        Scenario("actuated", "actuated", "shortest", "Actuated"),
        Scenario("ue", "actuated", "ue", "User equilibrium"),
        Scenario(
            "hive",
            "actuated",
            "hpdm",
            "Traffic Hive (HPDM)",
            cost_basis="marginal",
            epsilon=0.25,
        ),
        # Ablation: HPDM without the marginal-cost correction, i.e. softmax
        # over plain travel time. Retained to demonstrate that this
        # degenerates to Stochastic User Equilibrium.
        Scenario("hive_sue", "actuated", "hpdm", "HPDM (travel-time cost)"),
    ]
}


# --------------------------------------------------------------------------
# Demand
# --------------------------------------------------------------------------

def generate_demand(
    net_path: Path,
    out_path: Path,
    n_vehicles: int,
    horizon: int,
    seed: int,
) -> None:
    """Write a .rou.xml with `n_vehicles` trips over `horizon` seconds.

    Departure times are drawn from a peaked profile rather than uniformly,
    so the run exhibits the rush-hour surge the evaluation is about. Origins
    and destinations are sampled from fringe edges to force trips that
    traverse the corridor rather than terminating inside it.
    """
    import sumolib

    rng = random.Random(seed)
    net = sumolib.net.readNet(str(net_path))

    fringe = [
        e for e in net.getEdges()
        if e.allows("passenger") and (len(e.getIncoming()) == 0 or len(e.getOutgoing()) == 0)
    ]
    if len(fringe) < 2:
        fringe = [e for e in net.getEdges() if e.allows("passenger")]

    sources = [e for e in fringe if e.getOutgoing()]
    sinks = [e for e in fringe if e.getIncoming()]

    def peaked_depart() -> float:
        """Triangular profile peaking at 40% through the horizon."""
        return min(horizon - 1, max(0.0, rng.triangular(0, horizon, horizon * 0.4)))

    # Real OSM extracts are not strongly connected: one-way streets, clipped
    # boundaries and stranded fragments mean many origin-destination pairs
    # have no legal path. Validate each pair before writing it, and cache the
    # verdict so the routing cost stays proportional to the number of distinct
    # pairs rather than the number of vehicles.
    routable: dict[tuple[str, str], bool] = {}

    def is_routable(src, dst) -> bool:
        key = (src.getID(), dst.getID())
        cached = routable.get(key)
        if cached is not None:
            return cached
        try:
            path, cost = net.getShortestPath(src, dst, vClass="passenger")
            ok = path is not None and cost < 1e9
        except Exception:
            ok = False
        routable[key] = ok
        return ok

    trips: list[tuple[float, str, str]] = []
    rejected = 0
    for _ in range(n_vehicles):
        for _attempt in range(40):
            src = rng.choice(sources)
            dst = rng.choice(sinks)
            if dst.getID() == src.getID():
                continue
            if is_routable(src, dst):
                trips.append((peaked_depart(), src.getID(), dst.getID()))
                break
            rejected += 1
        else:
            continue

    if not trips:
        raise SystemExit(
            "No routable origin-destination pairs found. The network is likely "
            "fragmented — inspect it with netedit, or re-clip the OSM extract "
            "to a contiguous area."
        )

    if len(trips) < n_vehicles:
        print(
            f"  warning: only {len(trips)} of {n_vehicles} trips were routable "
            f"({rejected} pairs rejected). The network may be fragmented."
        )

    trips.sort(key=lambda t: t[0])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        fh.write(
            '  <vType id="car" vClass="passenger" accel="2.6" decel="4.5" '
            'sigma="0.5" length="4.6" minGap="2.0" maxSpeed="27.8" '
            'carFollowModel="Krauss"/>\n'
        )
        fh.write(
            '  <vType id="jeepney" vClass="bus" accel="1.6" decel="4.0" '
            'sigma="0.7" length="7.0" minGap="2.5" maxSpeed="19.4" '
            'carFollowModel="Krauss"/>\n'
        )
        fh.write(
            '  <vType id="motorcycle" vClass="motorcycle" accel="3.5" decel="5.5" '
            'sigma="0.8" length="2.2" minGap="1.0" maxSpeed="25.0" '
            'carFollowModel="Krauss" latAlignment="arbitrary"/>\n'
        )
        for i, (depart, src, dst) in enumerate(trips):
            # Mix reflects Metro Manila composition rather than pure passenger
            roll = rng.random()
            vtype = "motorcycle" if roll < 0.32 else ("jeepney" if roll < 0.44 else "car")
            fh.write(
                f'  <trip id="v{i}" type="{vtype}" depart="{depart:.2f}" '
                f'from="{src}" to="{dst}"/>\n'
            )
        fh.write("</routes>\n")

    print(f"wrote {out_path} ({n_vehicles} trips, horizon {horizon}s, seed {seed})")


# --------------------------------------------------------------------------
# Signal programme override
# --------------------------------------------------------------------------

def write_tls_override(net_path: Path, out_path: Path, tls_type: str) -> None:
    """Emit an additional-file forcing every tlLogic to `tls_type`.

    Overriding via an additional file rather than editing the network keeps
    a single network file shared across all scenarios, which is what makes
    the comparison controlled.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<additional>\n')
        for logic in root.findall("tlLogic"):
            tl_id = logic.get("id")
            fh.write(
                f'  <tlLogic id="{tl_id}" type="{tls_type}" '
                f'programID="hive" offset="0">\n'
            )
            for phase in logic.findall("phase"):
                dur = phase.get("duration")
                state = phase.get("state")
                extra = ""
                if tls_type == "actuated" and "G" in state:
                    extra = ' minDur="6" maxDur="45"'
                fh.write(f'    <phase duration="{dur}" state="{state}"{extra}/>\n')
            fh.write("  </tlLogic>\n")
        fh.write("</additional>\n")


# --------------------------------------------------------------------------
# Routing policies
# --------------------------------------------------------------------------

def softmax_choice(costs: list[float], eta: float, rng: random.Random) -> int:
    """Return an index sampled from softmax(-eta * cost).

    eta controls sharpness. As eta grows the distribution collapses onto the
    argmin and HPDM degenerates into greedy user-equilibrium routing, which
    is exactly the failure mode the ablation is designed to detect. Keep
    eta small (0.03 validated) so alternates retain non-trivial mass.
    """
    if not costs:
        return 0
    lo = min(costs)
    weights = [math.exp(-eta * (c - lo)) for c in costs]
    total = sum(weights)
    if total <= 0 or not math.isfinite(total):
        return costs.index(lo)
    draw = rng.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if draw <= acc:
            return i
    return len(costs) - 1


class RoutingController:
    """Applies a routing rule to in-network vehicles each decision epoch."""

    #: BPR calibration constant. Standard US Bureau of Public Roads value;
    #: see the calibration note in the README for fitting this from SUMO.
    BETA = 0.15
    #: BPR exponent.
    BPR_N = 4
    #: Effective vehicles per metre of lane at practical capacity. A jam
    #: density of roughly one vehicle per 7 m at 40 percent occupancy.
    VEH_PER_M = 0.4 / 7.0

    def __init__(self, scenario: Scenario, seed: int, net_path: Path) -> None:
        self.scenario = scenario
        self.rng = random.Random(seed + 7919)
        self._assigned: set[str] = set()
        self._t0: dict[str, float] = {}
        self._cap: dict[str, float] = {}
        self._load_bpr_parameters(net_path)

    def _load_bpr_parameters(self, net_path: Path) -> None:
        """Precompute per-edge free-flow time and effective capacity.

        Both are read once from the network rather than queried per decision:
        free-flow time is length over speed limit, and effective capacity is
        lane count times length times practical density. Computing these on
        every routing epoch would dominate the controller's cost without
        changing the values.
        """
        import sumolib

        net = sumolib.net.readNet(str(net_path))
        for edge in net.getEdges():
            eid = edge.getID()
            speed = max(edge.getSpeed(), 1.0)
            length = max(edge.getLength(), 1.0)
            lanes = max(len(edge.getLanes()), 1)
            self._t0[eid] = length / speed
            self._cap[eid] = max(lanes * length * self.VEH_PER_M, 1.0)

    def step(self, traci, t: int) -> None:
        mode = self.scenario.routing
        if mode == "shortest":
            return
        if t % self.scenario.reroute_period != 0:
            return

        for vid in traci.vehicle.getIDList():
            if mode == "ue":
                # Greedy: always take the currently cheapest path. This is
                # what produces route flapping and herding.
                traci.vehicle.rerouteTraveltime(vid, currentTravelTimes=True)
            elif mode == "hpdm":
                self._assign_hpdm(traci, vid)

    def _assign_hpdm(self, traci, vid: str) -> None:
        """Softmax assignment over candidate routes to the vehicle's target.

        Each vehicle is assigned once, at first decision epoch after entry.
        Re-drawing every epoch would reintroduce the oscillation that the
        softmax is meant to eliminate.
        """
        if vid in self._assigned:
            return
        try:
            target = traci.vehicle.getRoute(vid)[-1]
            current = traci.vehicle.getRoadID(vid)
        except traci.TraCIException:
            return
        if current.startswith(":"):
            return

        candidates = self._candidate_routes(traci, current, target)
        if len(candidates) <= 1:
            self._assigned.add(vid)
            return

        costs = [self._route_cost(traci, r) for r in candidates]

        # Fairness constraint. Penalising the spread of travel times has a
        # degenerate minimiser — concentrating all demand on one corridor
        # gives every vehicle an identical, uniformly poor time and so zero
        # variance. Constraining the feasible set instead cannot be gamed
        # that way: a concentrated allocation drives the loaded corridor
        # past the tolerance and is excluded outright.
        if self.scenario.epsilon is not None and costs:
            floor = min(costs)
            limit = floor * (1.0 + self.scenario.epsilon)
            kept = [(r, c) for r, c in zip(candidates, costs) if c <= limit]
            if kept:
                candidates = [r for r, _ in kept]
                costs = [c for _, c in kept]

        pick = softmax_choice(costs, self.scenario.eta, self.rng)
        try:
            traci.vehicle.setRoute(vid, candidates[pick])
            self._assigned.add(vid)
        except traci.TraCIException:
            pass

    @staticmethod
    def _candidate_routes(traci, src: str, dst: str) -> list[tuple[str, ...]]:
        """Fastest route plus perturbed alternates.

        Alternates are produced by temporarily inflating the cost of the
        fastest route's interior edges, which surfaces genuinely disjoint
        corridors rather than near-identical variants.
        """
        try:
            best = tuple(traci.simulation.findRoute(src, dst).edges)
        except traci.TraCIException:
            return []
        if not best:
            return []
        routes = [best]
        interior = best[1:-1]
        if len(interior) >= 2:
            mid = interior[len(interior) // 2]
            try:
                original = traci.edge.getAdaptedTraveltime(mid, 0)
                traci.edge.adaptTraveltime(mid, 9_000.0)
                alt = tuple(traci.simulation.findRoute(src, dst).edges)
                if original >= 0:
                    traci.edge.adaptTraveltime(mid, original)
                else:
                    traci.edge.adaptTraveltime(mid, traci.edge.getTraveltime(mid))
                if alt and alt != best:
                    routes.append(alt)
            except traci.TraCIException:
                pass
        return routes

    def _route_cost(self, traci, route: tuple[str, ...]) -> float:
        """Cost of a route under the scenario's cost basis, in seconds.

        Two bases are supported, and the distinction is the whole point of
        the mechanism.

        ``travel_time`` sums the travel time each edge currently exhibits.
        A softmax over this quantity converges to Stochastic User
        Equilibrium: every vehicle weighs only what the route costs *it*,
        which is precisely the selfish objective the study aims to improve
        upon. Retained as an ablation.

        ``marginal`` sums the marginal social cost instead. For the BPR
        function

            T(N) = T0 [ 1 + beta (N/C)^n ]

        total edge cost is Z(N) = N T(N) = T0 N + beta T0 N^(n+1) / C^n, and
        so

            MC(N) = dZ/dN = T0 [ 1 + (n+1) beta (N/C)^n ]

        The congestion coefficient is multiplied by (n+1) — with n = 4, beta
        rises from 0.15 to 0.75. That surplus is the externality each
        additional vehicle imposes on those already on the edge. Assigning
        on this quantity internalises the externality, which is the standard
        realisation of Wardrop's second principle and yields System Optimum
        rather than User Equilibrium.
        """
        total = 0.0
        marginal = self.scenario.cost_basis == "marginal"
        for edge in route:
            if edge.startswith(":"):
                continue
            try:
                if not marginal:
                    total += traci.edge.getTraveltime(edge)
                    continue
                load = traci.edge.getLastStepVehicleNumber(edge)
                t0 = self._t0.get(edge)
                cap = self._cap.get(edge)
                if t0 is None or cap is None:
                    total += traci.edge.getTraveltime(edge)
                    continue
                ratio = load / cap
                total += t0 * (1.0 + (self.BPR_N + 1) * self.BETA * ratio ** self.BPR_N)
            except traci.TraCIException:
                continue
        return total


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def run(
    scenario: Scenario,
    n_vehicles: int,
    horizon: int,
    seed: int,
    net_path: Path = NET,
    fcd_period: float = 0.5,
) -> dict[str, Path]:
    """Run one scenario headless and return the paths of its outputs."""
    import traci

    out_dir = OUT / scenario.name
    out_dir.mkdir(parents=True, exist_ok=True)

    routes = out_dir / "demand.rou.xml"
    generate_demand(net_path, routes, n_vehicles, horizon, seed)

    tls_add = out_dir / "tls.add.xml"
    write_tls_override(net_path, tls_add, scenario.tls_type)

    paths = {
        "fcd": out_dir / "fcd.xml",
        "tripinfo": out_dir / "tripinfo.xml",
        "summary": out_dir / "summary.xml",
    }

    cmd = [
        "sumo",
        "-n", str(net_path),
        "-r", str(routes),
        "-a", str(tls_add),
        "--begin", "0",
        "--end", str(horizon + 600),
        "--step-length", "0.5",
        "--fcd-output", str(paths["fcd"]),
        "--device.fcd.period", str(fcd_period),
        "--tripinfo-output", str(paths["tripinfo"]),
        "--summary-output", str(paths["summary"]),
        "--seed", str(seed),
        "--time-to-teleport", "300",
        # A single unroutable trip should degrade the run, not abort it.
        # Rejected vehicles are reported in the run summary.
        "--ignore-route-errors",
        "--no-step-log",
        "--no-warnings",
        "--duration-log.statistics",
        "--waiting-time-memory", "10000",
    ]

    needs_traci = scenario.routing != "shortest"
    if not needs_traci:
        print(f"[{scenario.name}] running headless (no TraCI)")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr[-2000:], file=sys.stderr)
            sys.exit(f"sumo failed for scenario {scenario.name}")
    else:
        print(f"[{scenario.name}] running headless with {scenario.routing} controller")
        traci.start(cmd)
        controller = RoutingController(scenario, seed, net_path)
        step = 0
        try:
            while traci.simulation.getMinExpectedNumber() > 0:
                traci.simulationStep()
                controller.step(traci, int(traci.simulation.getTime()))
                step += 1
                if step > (horizon + 600) * 2:
                    break
        finally:
            traci.close()

    print(f"[{scenario.name}] done -> {out_dir}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="hive")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--vehicles", type=int, default=1000)
    parser.add_argument("--horizon", type=int, default=900)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--net", type=Path, default=NET)
    args = parser.parse_args()

    targets = list(SCENARIOS.values()) if args.all else [SCENARIOS[args.scenario]]
    for scenario in targets:
        run(scenario, args.vehicles, args.horizon, args.seed, args.net)


if __name__ == "__main__":
    main()
