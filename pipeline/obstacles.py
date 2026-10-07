"""Roadside obstacles applied inside the SUMO run.

Metro Manila corridors lose capacity to things a clean OSM network does not
contain: sidewalk vendors spilling into the curb lane, jeepneys loading
mid-block, enforcers holding an intersection by hand, stalled vehicles and
roadworks. This module turns a list of such obstacles into TraCI actions so
they shape the trajectories SUMO produces, rather than being drawn on top of
a run they never affected.

Because every routing rule in run_scenarios reads edge travel times from
TraCI, an obstacle that slows or closes a lane raises that edge's cost and
the Hive mechanism routes around it on its next decision. Nothing in the
controllers is told where the obstacles are.

Obstacle schema (JSON; coordinates in the net.json local metre grid, origin
at the network centre, y up):

    {
      "id": "o1",
      "type": "vendor" | "jeepney_stop" | "enforcer" | "stalled" | "roadwork",
      "label": "Fruit vendors",          # optional, shown in the app
      "x": -220.0, "y": -104.0,
      "start_s": 0, "end_s": 900,        # active window, simulation seconds
      "severity": 1 | 2 | 3              # light / moderate / heavy
    }

Modelling choices (stated so they can be defended):

  vendor        curb lane max speed scaled to 60 / 40 / 25 % of its limit;
                at severity 3 on a multi-lane edge the curb lane is closed.
  jeepney_stop  every jeepney whose route crosses the edge is given a stop
                on the curb lane at that position for 15 / 30 / 60 s.
  enforcer      at a signalised junction, each green phase is held for
                1.5 / 2.0 / 2.5 times its programmed duration (manual
                direction favours long phases); at an unsignalised junction
                the approach lanes are slowed to 50 % instead.
  stalled       the curb lane is closed (single-lane edge: slowed to 3 m/s).
  roadwork      the curb lane is closed and the adjacent lane slowed to 50 %.

Lane-level speed and permission changes apply to the whole lane, which is
coarser than a vendor stall a few metres long. On short urban links the
difference is small; on long links prefer splitting the edge in netedit.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

TYPES = ("vendor", "jeepney_stop", "enforcer", "stalled", "roadwork")
VENDOR_SPEED = {1: 0.60, 2: 0.40, 3: 0.25}
JEEP_DWELL = {1: 15.0, 2: 30.0, 3: 60.0}
ENFORCER_HOLD = {1: 1.5, 2: 2.0, 3: 2.5}
ALL_CLASSES = ["passenger", "bus", "motorcycle", "truck", "taxi", "delivery"]


@dataclass
class Obstacle:
    id: str
    type: str
    x: float
    y: float
    start_s: float = 0.0
    end_s: float = 1e9
    severity: int = 2
    label: str = ""
    # resolved against the network
    edge: str | None = None
    lane: str | None = None
    lanes: list[str] = field(default_factory=list)
    pos: float | None = None
    tls: str | None = None
    note: str = ""
    # run statistics
    active: bool = False
    jeepney_stops: int = 0
    held_phases: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "Obstacle":
        t = str(d.get("type", "vendor"))
        if t not in TYPES:
            raise ValueError(f"unknown obstacle type: {t}")
        sev = int(d.get("severity", 2))
        return cls(
            id=str(d.get("id") or f"o{abs(hash((d.get('x'), d.get('y')))) % 10**6}"),
            type=t,
            x=float(d["x"]),
            y=float(d["y"]),
            start_s=float(d.get("start_s", 0.0)),
            end_s=float(d.get("end_s", 1e9)),
            severity=min(3, max(1, sev)),
            label=str(d.get("label", "")),
        )

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "active"}


def load(path_or_list) -> list[Obstacle]:
    """Accept a path to a JSON file, a JSON string, or a list of dicts."""
    if path_or_list is None:
        return []
    if isinstance(path_or_list, (str, Path)) and Path(path_or_list).exists():
        data = json.loads(Path(path_or_list).read_text())
    elif isinstance(path_or_list, str):
        data = json.loads(path_or_list)
    else:
        data = path_or_list
    if isinstance(data, dict):
        data = data.get("obstacles", [])
    return [Obstacle.from_dict(d) for d in data]


def resolve(obstacles: list[Obstacle], net_path: Path) -> list[Obstacle]:
    """Snap each obstacle to the lane, position or junction it acts on."""
    import sumolib

    net = sumolib.net.readNet(str(net_path))
    xmin, ymin, xmax, ymax = net.getBoundary()
    cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0

    for ob in obstacles:
        x, y = ob.x + cx, ob.y + cy
        if ob.type == "enforcer":
            best, bd = None, 60.0
            for node in net.getNodes():
                nx, ny = node.getCoord()
                d = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
                if d < bd and node.getIncoming():
                    best, bd = node, d
            if best is None:
                ob.note = "no junction within 60 m; ignored"
                continue
            if best.getType() == "traffic_light":
                ob.tls = best.getID()
            ob.lanes = [l.getID() for e in best.getIncoming() for l in e.getLanes()
                        if l.allows("passenger")]
            ob.edge = None
            continue

        cands = [(lane, d) for lane, d in net.getNeighboringLanes(x, y, 25.0)
                 if lane.allows("passenger") and lane.getEdge().getFunction() != "internal"]
        if not cands:
            ob.note = "no drivable lane within 25 m; ignored"
            continue
        lane, _ = min(cands, key=lambda c: c[1])
        edge = lane.getEdge()
        curb = next((l for l in edge.getLanes() if l.allows("passenger")), lane)
        pos, _ = curb.getClosestLanePosAndDist((x, y))
        ob.edge = edge.getID()
        ob.lane = curb.getID()
        ob.lanes = [l.getID() for l in edge.getLanes() if l.allows("passenger")]
        ob.pos = float(min(max(pos, 5.0), max(5.0, curb.getLength() - 5.0)))
    return obstacles


class ObstacleController:
    """Applies and reverts obstacle effects as simulation time passes."""

    def __init__(self, obstacles: list[Obstacle]) -> None:
        self.obstacles = [o for o in obstacles if o.edge or o.lanes]
        self._saved_speed: dict[str, float] = {}
        self._saved_allowed: dict[str, list[str]] = {}
        self._tls_phase: dict[str, int] = {}
        self._jeep_seen: set[tuple[str, str]] = set()

    # -- lane helpers ------------------------------------------------------
    def _slow(self, traci, lane: str, factor: float) -> None:
        if lane not in self._saved_speed:
            self._saved_speed[lane] = traci.lane.getMaxSpeed(lane)
        traci.lane.setMaxSpeed(lane, max(1.0, self._saved_speed[lane] * factor))

    def _close(self, traci, lane: str) -> None:
        if lane not in self._saved_allowed:
            self._saved_allowed[lane] = list(traci.lane.getAllowed(lane))
        traci.lane.setDisallowed(lane, ALL_CLASSES)

    def _restore(self, traci, lane: str) -> None:
        if lane in self._saved_speed:
            traci.lane.setMaxSpeed(lane, self._saved_speed.pop(lane))
        if lane in self._saved_allowed:
            allowed = self._saved_allowed.pop(lane)
            if allowed:
                traci.lane.setAllowed(lane, allowed)
            else:
                traci.lane.setDisallowed(lane, [])

    # -- lifecycle ---------------------------------------------------------
    def _activate(self, traci, ob: Obstacle) -> None:
        multi = len(ob.lanes) > 1
        if ob.type == "vendor":
            if ob.severity == 3 and multi:
                self._close(traci, ob.lane)
            else:
                self._slow(traci, ob.lane, VENDOR_SPEED[ob.severity])
        elif ob.type == "stalled":
            if multi:
                self._close(traci, ob.lane)
            else:
                self._slow(traci, ob.lane, 3.0 / max(1.0, traci.lane.getMaxSpeed(ob.lane)))
        elif ob.type == "roadwork":
            if multi:
                self._close(traci, ob.lane)
                self._slow(traci, ob.lanes[1], 0.5)
            else:
                self._slow(traci, ob.lane, 0.3)
        elif ob.type == "enforcer" and not ob.tls:
            for lane in ob.lanes:
                self._slow(traci, lane, 0.5)
        ob.active = True

    def _deactivate(self, traci, ob: Obstacle) -> None:
        for lane in ([ob.lane] if ob.lane else []) + ob.lanes:
            self._restore(traci, lane)
        ob.active = False

    def _jeepney_stops(self, traci, ob: Obstacle, vids) -> None:
        for vid in vids:
            key = (ob.id, vid)
            if key in self._jeep_seen:
                continue
            try:
                if traci.vehicle.getTypeID(vid).split("@")[0] != "jeepney":
                    self._jeep_seen.add(key)
                    continue
                route = traci.vehicle.getRoute(vid)
                idx = traci.vehicle.getRouteIndex(vid)
                if ob.edge not in route[max(0, idx):]:
                    continue
                traci.vehicle.setStop(vid, ob.edge, pos=ob.pos,
                                      laneIndex=int(ob.lane.rsplit("_", 1)[1]),
                                      duration=JEEP_DWELL[ob.severity])
                ob.jeepney_stops += 1
            except Exception:
                pass  # too close to brake, already past, or route changed
            self._jeep_seen.add(key)

    def step(self, traci, t: float) -> None:
        departed = traci.simulation.getDepartedIDList()
        for ob in self.obstacles:
            live = ob.start_s <= t < ob.end_s
            if live and not ob.active:
                self._activate(traci, ob)
                if ob.type == "jeepney_stop":
                    self._jeepney_stops(traci, ob, traci.vehicle.getIDList())
            elif not live and ob.active:
                self._deactivate(traci, ob)
            if not live:
                continue
            if ob.type == "jeepney_stop":
                # Routes change under the Hive controller, so re-check jeepneys
                # entering the network and those just rerouted onto the edge.
                self._jeepney_stops(traci, ob, departed)
                if int(t * 2) % 20 == 0:
                    self._jeepney_stops(traci, ob, traci.vehicle.getIDList())
            elif ob.type == "enforcer" and ob.tls:
                phase = traci.trafficlight.getPhase(ob.tls)
                if self._tls_phase.get(ob.tls) != phase:
                    self._tls_phase[ob.tls] = phase
                    state = traci.trafficlight.getRedYellowGreenState(ob.tls)
                    if "G" in state:
                        base = traci.trafficlight.getPhaseDuration(ob.tls)
                        traci.trafficlight.setPhaseDuration(
                            ob.tls, base * ENFORCER_HOLD[ob.severity])
                        ob.held_phases += 1

    def report(self) -> list[dict]:
        return [o.public() for o in self.obstacles]


def write_report(obstacles: list[Obstacle], path: Path) -> None:
    path.write_text(json.dumps({"obstacles": [o.public() for o in obstacles]}, indent=2))
