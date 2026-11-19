"""Real-time digital-twin mode.

The batch path (server.app /run) simulates a whole scenario first and the
app replays it afterwards. Live mode runs SUMO step by step while the page
is open and streams every vehicle as it moves, so the presenter can act on
the network and watch the routing rules respond:

  * two simulations run side by side on identical demand (by default
    selfish routing and Traffic Hive with live rerouting);
  * an obstacle placed on the map is applied to both at the same moment;
  * each frame reports the decision time of each controller, which is the
    evidence that the rule keeps up with real time.

Endpoints (mounted by server.app):

    GET  /live/net    road geometry in the local metre grid (pack.export_network)
    WS   /live/ws     commands in, frames out

Commands (JSON over the socket):

    {"cmd": "start", "vehicles": 1000, "horizon": 900, "seed": 11,
     "speed": 10, "rules": ["ue_corridor", "hive_reroute_corridor"]}
    {"cmd": "pause"} / {"cmd": "resume"} / {"cmd": "stop"}
    {"cmd": "speed", "value": 20}          # simulated seconds per wall second
    {"cmd": "obstacle", "type": "stalled", "x": 120.0, "y": -40.0,
     "severity": 3, "duration": 600, "label": "Stalled jeepney"}
    {"cmd": "preset"}                       # the twelve España obstacles
    {"cmd": "remove", "id": "live3"}        # end one obstacle
    {"cmd": "clear"}                        # end every active obstacle
    {"cmd": "follow", "vid": "veh42"}       # report one driver in detail (null to stop)

    Obstacle types are those of pipeline.obstacles: vendor, jeepney_stop,
    enforcer, stalled, roadwork; severity 1-3.

Frames:

    {"type": "frame", "t": 312.0, "lanes": [{"rule", "label", "running",
     "arrived", "pending", "speed_kmh", "halting", "vehicle_hours",
     "decision_ms" (mean per step, last minute), "decision_ms_max" (95th
     percentile, last minute), "reroutes", "share",
     "veh": [x0, y0, c0, x1, y1, c1, ...], "ids": [vid0, vid1, ...],
     "follow": {...} or null}], "obstacles": [...]}

    Both simulations load the same demand file, so a vehicle id names the
    same driver (same origin, destination and departure time) in each, and
    "follow" lets the page show one person's trip under both rules.

    c encodes what the vehicle is doing for colouring: 0, 1, 2 = travelling
    on corridor 1, 2 or 3 of its origin; 3 = rerouted en route; +4 when the
    vehicle is stopped.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import traci
import traci.constants as tc
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pipeline import obstacles as obstacle_model
from pipeline import pack
from pipeline.run_scenarios import (
    NET,
    SCENARIOS,
    CorridorController,
    HiveRerouteController,
    generate_demand,
    write_tls_override,
)

CORRIDORS = Path("data/net/corridors.json")
LIVE_DIR = Path("data/live")
PRESET = Path("examples/obstacles_espana_demo.json")
STEP_S = 0.5
FRAME_EVERY = 2          # steps per frame: one frame per simulated second
CLEARANCE_S = 1800
LIVE_RULES = ("ue_corridor", "hive_corridor", "ue_reroute_corridor", "hive_reroute_corridor")

router = APIRouter(prefix="/live")
_net_cache: dict = {}


class _Conn:
    """A TraCI connection that also exposes TraCIException.

    The controllers are written against the traci module (they catch
    traci.TraCIException); a labelled connection lacks that attribute, so
    this wrapper adds it and forwards everything else.
    """

    TraCIException = traci.TraCIException

    def __init__(self, conn) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _sumolib_net():
    if "net" not in _net_cache:
        import sumolib
        _net_cache["net"] = sumolib.net.readNet(str(NET))
        xmin, ymin, xmax, ymax = _net_cache["net"].getBoundary()
        _net_cache["centre"] = ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)
    return _net_cache["net"]


def _controller(rule: str, seed: int) -> CorridorController:
    scenario = SCENARIOS[rule]
    if scenario.routing in ("corridor_reroute", "corridor_ue_reroute"):
        return HiveRerouteController(scenario, seed, CORRIDORS)
    if scenario.routing in ("corridor", "corridor_ue"):
        return CorridorController(scenario, seed, CORRIDORS)
    raise ValueError(f"{rule} is not available in live mode")


class _Lane:
    """One running simulation and its bookkeeping."""

    def __init__(self, rule: str, index: int, cmd: list[str], seed: int, tag: str) -> None:
        self.rule = rule
        self.label = SCENARIOS[rule].label
        self.label_id = f"live-{tag}-{index}"
        traci.start(cmd, label=self.label_id)
        self.conn = _Conn(traci.getConnection(self.label_id))
        self.controller = _controller(rule, seed)
        self.obstacles = obstacle_model.ObstacleController([])
        self.arrived = 0
        self.vehsec = 0.0
        self.decision_ms: list[float] = []
        self.corridor_of: dict[str, int] = {}
        self.route_index = [{r: k for k, r in enumerate(g)} for g in self.controller.routes]
        self.done = False
        self.now = 0.0
        self.trip: dict[str, dict] = {}     # departure and arrival times, for the followed driver

    def step(self, now_hint: float) -> None:
        c = self.conn
        c.simulationStep()
        now = c.simulation.getTime()
        self.now = now
        if self.obstacles.obstacles:
            self.obstacles.step(c, now)
        start = time.perf_counter()
        self.controller.step(c, int(now))
        self.decision_ms.append((time.perf_counter() - start) * 1000)
        for vid in c.simulation.getDepartedIDList():
            c.vehicle.subscribe(vid, [tc.VAR_POSITION, tc.VAR_SPEED, tc.VAR_ANGLE])
            self.trip[vid] = {"depart": now}
            try:
                route = tuple(c.vehicle.getRoute(vid))
                gi = self.controller._origin_to_group.get((route[0], route[-1]))
                k = self.route_index[gi].get(route) if gi is not None else None
                self.corridor_of[vid] = k if k is not None else 0
            except (traci.TraCIException, IndexError):
                self.corridor_of[vid] = 0
        arrived = c.simulation.getArrivedIDList()
        self.arrived += len(arrived)
        for vid in arrived:
            self.corridor_of.pop(vid, None)
            if vid in self.trip:
                self.trip[vid]["arrive"] = now
        running = c.vehicle.getIDCount()
        try:
            pending = len(c.simulation.getPendingVehicles())
        except (traci.TraCIException, AttributeError):
            pending = 0
        self.vehsec += (running + pending) * STEP_S
        if c.simulation.getMinExpectedNumber() <= 0:
            self.done = True

    def follow(self, vid: str | None, obstacles: list) -> dict | None:
        """Everything the driver's phone shows for one vehicle."""
        if not vid:
            return None
        c, net = self.conn, _sumolib_net()
        trip = self.trip.get(vid)
        if trip is None:
            return {"vid": vid, "state": "waiting"}
        if "arrive" in trip:
            return {"vid": vid, "state": "arrived", "trip_s": round(trip["arrive"] - trip["depart"], 1),
                    "rerouted": getattr(self.controller, "_switches", {}).get(vid, 0),
                    "saved_s": round(getattr(self.controller, "_saved_s", {}).get(vid, 0.0))}
        try:
            route = c.vehicle.getRoute(vid)
            idx = c.vehicle.getRouteIndex(vid)
            road = c.vehicle.getRoadID(vid)
            pos = c.vehicle.getLanePosition(vid)
            speed = c.vehicle.getSpeed(vid)
            x, y = c.vehicle.getPosition(vid)
            angle = c.vehicle.getAngle(vid)
            limit = c.vehicle.getAllowedSpeed(vid)
        except traci.TraCIException:
            return {"vid": vid, "state": "gone"}
        cx, cy = _net_cache["centre"]
        rest = list(route[max(0, idx):])

        def name(e: str) -> str:
            try:
                return net.getEdge(e).getName() or ""
            except KeyError:
                return ""

        def length(e: str) -> float:
            try:
                return net.getEdge(e).getLength()
            except KeyError:
                return 0.0

        remaining = max(0.0, sum(length(e) for e in rest) - (pos if rest and road == rest[0] else 0.0))
        t_rest = 0.0
        for e in rest:
            try:
                t_rest += min(300.0, c.edge.getTraveltime(e))
            except traci.TraCIException:
                pass
        here = name(road) if not road.startswith(":") else (name(rest[0]) if rest else "")
        nxt, dist, seen = None, 0.0, here
        for k, e in enumerate(rest):
            seg = length(e) - (pos if k == 0 and road == e else 0.0)
            n = name(e)
            if n and n != seen:
                nxt = {"street": n, "in_m": round(dist)}
                break
            dist += max(0.0, seg)
        ahead, along = [], 0.0
        index = {e: k for k, e in enumerate(rest)}
        for o in obstacles:
            if o.get("edge") in index:
                k = index[o["edge"]]
                d = sum(length(e) for e in rest[:k]) - (pos if rest and road == rest[0] else 0.0)
                ahead.append({"type": o["type"], "label": o.get("label") or "", "in_m": round(max(0.0, d))})
        ahead.sort(key=lambda a: a["in_m"])
        gi = self.controller._origin_to_group.get((route[0], route[-1]))
        dest = name(route[-1]) or "destination"
        return {
            "vid": vid, "state": "driving",
            "x": round(x - cx, 1), "y": round(y - cy, 1), "angle": round(angle, 1),
            "speed_kmh": round(speed * 3.6, 1), "limit_kmh": round(limit * 3.6),
            "street": here, "next": nxt, "to": dest,
            "remaining_m": round(remaining), "remaining_s": round(t_rest),
            "elapsed_s": round(self.now - trip["depart"], 1),
            "corridor": self.corridor_of.get(vid, 0), "group": gi,
            "route": rest[:80],
            "ahead": ahead[:3],
            "rerouted": getattr(self.controller, "_switches", {}).get(vid, 0),
            "saved_s": round(getattr(self.controller, "_saved_s", {}).get(vid, 0.0)),
        }

    def frame(self, follow_id: str | None = None, obstacles: list | None = None) -> dict:
        c = self.conn
        cx, cy = _net_cache["centre"]
        subs = c.vehicle.getAllSubscriptionResults()
        rerouted = getattr(self.controller, "_switches", {})
        veh: list[int] = []
        ids: list[str] = []
        speeds, halting = 0.0, 0
        for vid, vals in subs.items():
            ids.append(vid)
            x, y = vals[tc.VAR_POSITION]
            v = vals[tc.VAR_SPEED]
            speeds += v
            code = 3 if vid in rerouted else self.corridor_of.get(vid, 0)
            if v < 0.1:
                halting += 1
                code += 4
            veh.extend((int(round(x - cx)), int(round(y - cy)), code))
        n = len(subs)
        # decision work over the last minute of simulated time: mean per
        # half-second step and 95th percentile (refreshes and reviews happen
        # every 10 s and 30 s, so single steps are mostly near zero)
        recent = self.decision_ms[-120:] or [0.0]
        summary = self.controller.summary()
        try:
            pending = len(c.simulation.getPendingVehicles())
        except (traci.TraCIException, AttributeError):
            pending = 0
        return {
            "rule": self.rule,
            "label": self.label,
            "running": n,
            "arrived": self.arrived,
            "pending": pending,
            "speed_kmh": round(speeds / n * 3.6, 1) if n else 0.0,
            "halting": halting,
            "vehicle_hours": round(self.vehsec / 3600.0, 2),
            "decision_ms": round(sum(recent) / len(recent), 2),
            # 95th percentile over the last minute of simulated time; the
            # first steps include SUMO warm-up and are not decision work
            "decision_ms_max": round(sorted(recent)[int(0.95 * (len(recent) - 1))], 1),
            "reroutes": getattr(self.controller, "reroutes", 0),
            "share": summary.get("share", []),
            "veh": veh,
            "ids": ids,
            "follow": self.follow(follow_id, obstacles or []),
        }

    def close(self) -> None:
        try:
            traci.switch(self.label_id)
            traci.close()
        except Exception:
            pass


class LiveRun(threading.Thread):
    """Runs the side-by-side simulations and paces them to the wall clock."""

    def __init__(self, params: dict, emit) -> None:
        super().__init__(daemon=True)
        self.params = params
        self.emit = emit
        self.commands: "queue.Queue[dict]" = queue.Queue()
        self.speed = float(params.get("speed", 10))
        self.paused = False
        self.stopped = False
        self.obstacle_log: list[dict] = []
        self.follow_id: str | None = None

    def _setup(self) -> list[_Lane]:
        vehicles = int(self.params.get("vehicles", 1000))
        horizon = int(self.params.get("horizon", 900))
        seed = int(self.params.get("seed", 11))
        rules = [r for r in self.params.get("rules", ["ue_corridor", "hive_reroute_corridor"]) if r in LIVE_RULES][:2]
        if not rules:
            rules = ["ue_corridor", "hive_reroute_corridor"]
        tag = f"{int(time.time() * 1000) % 10**8}"
        out = LIVE_DIR / tag
        out.mkdir(parents=True, exist_ok=True)
        cdata = json.loads(CORRIDORS.read_text())
        groups = cdata.get("groups") or [cdata]
        od = [(g["origin"], g["destination"]) for g in groups]
        routes = out / "demand.rou.xml"
        generate_demand(NET, routes, vehicles, horizon, seed, od=od)
        tls_add = out / "tls.add.xml"
        write_tls_override(NET, tls_add, "actuated")
        cmd = [
            "sumo", "-n", str(NET), "-r", str(routes), "-a", str(tls_add),
            "--begin", "0", "--end", str(horizon + CLEARANCE_S), "--step-length", str(STEP_S),
            "--seed", str(seed), "--time-to-teleport", "300", "--ignore-route-errors",
            "--no-step-log", "--no-warnings",
        ]
        _sumolib_net()
        return [_Lane(rule, i, cmd, seed, tag) for i, rule in enumerate(rules)]

    def _place(self, lanes: list[_Lane], spec: dict, now: float) -> None:
        duration = float(spec.get("duration", 600))
        if spec.get("type", "stalled") not in obstacle_model.TYPES:
            self.emit({"type": "notice", "message": f"Unknown obstacle type {spec.get('type')}."})
            return
        base = {
            "type": spec.get("type", "stalled"),
            "x": float(spec["x"]), "y": float(spec["y"]),
            "severity": int(spec.get("severity", 3)),
            "label": str(spec.get("label", "")),
            "start_s": now, "end_s": now + duration,
        }
        base["id"] = spec.get("id") or f"live{len(self.obstacle_log) + 1}"
        placed = None
        for lane in lanes:
            ob = obstacle_model.Obstacle.from_dict(base)
            obstacle_model.resolve([ob], net=_sumolib_net())
            if ob.edge or ob.lanes:
                lane.obstacles.obstacles.append(ob)
                placed = ob
        if placed is not None:
            self.obstacle_log.append({**base, "edge": placed.edge, "note": placed.note})
        else:
            self.emit({"type": "notice", "message": "No road within 25 m of that point."})

    def _apply_command(self, lanes: list[_Lane], cmd: dict, now: float) -> None:
        kind = cmd.get("cmd")
        if kind == "pause":
            self.paused = True
        elif kind == "resume":
            self.paused = False
        elif kind == "stop":
            self.stopped = True
        elif kind == "speed":
            self.speed = max(0.5, min(60.0, float(cmd.get("value", 10))))
        elif kind == "obstacle":
            self._place(lanes, cmd, now)
        elif kind == "preset" and PRESET.exists():
            for spec in json.loads(PRESET.read_text()).get("obstacles", []):
                self._place(lanes, {**spec, "id": f"es-{spec.get('id', '')}-{len(self.obstacle_log)}",
                                    "duration": 1e6}, now)
        elif kind == "follow":
            self.follow_id = cmd.get("vid") or None
        elif kind == "remove":
            oid = cmd.get("id")
            for lane in lanes:
                for ob in lane.obstacles.obstacles:
                    if ob.id == oid:
                        ob.end_s = min(ob.end_s, now)
            for ob in self.obstacle_log:
                if ob["id"] == oid:
                    ob["end_s"] = min(ob["end_s"], now)
        elif kind == "clear":
            for lane in lanes:
                for ob in lane.obstacles.obstacles:
                    ob.end_s = min(ob.end_s, now)
            for ob in self.obstacle_log:
                ob["end_s"] = min(ob["end_s"], now)

    def run(self) -> None:
        lanes: list[_Lane] = []
        pool = None
        try:
            self.emit({"type": "status", "state": "starting"})
            lanes = self._setup()
            self.emit({"type": "status", "state": "running",
                       "rules": [{"rule": l.rule, "label": l.label} for l in lanes]})
            # Each simulation is a separate SUMO process; most of a step is
            # spent waiting on its socket, which releases the GIL, so
            # stepping the two from two threads nearly halves the wall time.
            pool = ThreadPoolExecutor(max_workers=len(lanes))
            sim_t, wall0, sim0, step = 0.0, time.perf_counter(), 0.0, 0
            end_t = int(self.params.get("horizon", 900)) + CLEARANCE_S
            while not self.stopped and not all(l.done for l in lanes) and sim_t < end_t:
                while True:
                    try:
                        self._apply_command(lanes, self.commands.get_nowait(), sim_t)
                        wall0, sim0 = time.perf_counter(), sim_t   # re-anchor pacing
                    except queue.Empty:
                        break
                if self.paused:
                    time.sleep(0.05)
                    wall0, sim0 = time.perf_counter(), sim_t
                    continue
                for fut in [pool.submit(lane.step, sim_t) for lane in lanes if not lane.done]:
                    fut.result()
                sim_t += STEP_S
                step += 1
                if step % FRAME_EVERY == 0:
                    active = [o for o in self.obstacle_log if o["start_s"] <= sim_t < o["end_s"]]
                    self.emit({
                        "type": "frame", "t": sim_t,
                        "lanes": [l.frame(self.follow_id, active) for l in lanes],
                        "obstacles": active,
                    })
                    ahead = (sim_t - sim0) / self.speed - (time.perf_counter() - wall0)
                    if ahead > 0:
                        time.sleep(ahead)
            self.emit({"type": "status", "state": "finished", "t": sim_t})
        except Exception as exc:  # reported to the page rather than lost
            self.emit({"type": "status", "state": "error", "message": str(exc)})
        finally:
            if pool is not None:
                pool.shutdown(wait=False)
            for lane in lanes:
                lane.close()


@router.get("/net")
def live_net() -> dict:
    """Road geometry for the live map, cached after the first request."""
    if "export" not in _net_cache:
        geo = pack.export_network(NET)
        _net_cache["export"] = {"bounds": geo["bounds"], "roads": geo["roads"],
                                "preset": json.loads(PRESET.read_text()).get("obstacles", []) if PRESET.exists() else []}
    return _net_cache["export"]


@router.websocket("/ws")
async def live_socket(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue = asyncio.Queue(maxsize=8)
    run: LiveRun | None = None

    def emit(msg: dict) -> None:
        def put() -> None:
            if msg.get("type") == "frame" and outbox.full():
                try:
                    outbox.get_nowait()          # drop the oldest frame, never block the simulation
                except asyncio.QueueEmpty:
                    pass
            try:
                outbox.put_nowait(msg)
            except asyncio.QueueFull:
                pass
        loop.call_soon_threadsafe(put)

    async def sender() -> None:
        while True:
            msg = await outbox.get()
            await ws.send_text(json.dumps(msg, separators=(",", ":")))

    send_task = asyncio.create_task(sender())
    try:
        while True:
            cmd = json.loads(await ws.receive_text())
            if cmd.get("cmd") == "start":
                if run is not None:
                    run.stopped = True
                run = LiveRun(cmd, emit)
                run.start()
            elif run is not None:
                run.commands.put(cmd)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if run is not None:
            run.stopped = True
        send_task.cancel()
