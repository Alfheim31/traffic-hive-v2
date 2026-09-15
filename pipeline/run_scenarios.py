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


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("fixed", "static", "shortest", "Fixed-time"),
        Scenario("actuated", "actuated", "shortest", "Actuated"),
        Scenario("ue", "actuated", "ue", "User equilibrium"),
        Scenario("hive", "actuated", "hpdm", "Traffic Hive (HPDM)"),
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

    trips: list[tuple[float, str, str]] = []
    for _ in range(n_vehicles):
        src = rng.choice(sources)
        dst = rng.choice(sinks)
        tries = 0
        while dst.getID() == src.getID() and tries < 10:
            dst = rng.choice(sinks)
            tries += 1
        trips.append((peaked_depart(), src.getID(), dst.getID()))

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

    def __init__(self, scenario: Scenario, seed: int) -> None:
        self.scenario = scenario
        self.rng = random.Random(seed + 7919)
        self._assigned: set[str] = set()

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

    @staticmethod
    def _route_cost(traci, route: tuple[str, ...]) -> float:
        """Sum of current travel times over the route's edges, in seconds."""
        total = 0.0
        for edge in route:
            if edge.startswith(":"):
                continue
            try:
                total += traci.edge.getTraveltime(edge)
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
        controller = RoutingController(scenario, seed)
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
