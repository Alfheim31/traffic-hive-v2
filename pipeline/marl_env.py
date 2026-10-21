"""Multi-agent environment for learning corridor assignment in SUMO.

Each origin-destination pair in data/net/corridors.json is one agent. Every
decision epoch (10 s of simulated time) an agent observes its own corridors
and a summary of the network, and chooses how the vehicles departing from
its origin during the next epoch are split across its K corridors.

Agents (i = 1..16), corridors k = 1..K (K = 3 on España)
--------------------------------------------------------
Observation o_i (30 values on España):
    for each corridor k:   T_k / T0_k - 1      measured delay ratio (capped)
                           N_k / C_k            occupancy
                           change in delay ratio since the previous epoch
    own vehicles in network / 50, own departures last epoch / 10
    network mean delay ratio, vehicles in network / 500, t / horizon
    one-hot agent id (lets one shared policy specialise per OD pair)

Action a_i: one of the splits on a 0.25 grid of the simplex
    (15 splits for K = 3, e.g. (1, 0, 0), (0.5, 0.25, 0.25), ...)

Reward r_i: cooperative and tied to the system-optimum objective. With
    D_i = vehicle-seconds accrued by agent i's vehicles this epoch, counting
          both vehicles in the network and vehicles whose departure time
          has passed but which SUMO could not yet insert,
    r_i = -(alpha * D_i + (1 - alpha) * mean_j D_j) / SCALE
          - lambda_f * sum_k p_ik * max(0, T_k / min_k T_k - (1 + epsilon))
    Summed over an episode, sum_i D_i is the total system travel time, the
    quantity a system-optimal assignment minimises. Vehicles still in the
    network when the episode is cut off are charged STRANDED_S seconds each
    (600 s, about one full trip on España), so a policy cannot lower its
    cost by trapping traffic. The second term
    is the thesis' fairness tolerance, applied to expected travel time.

State s (for the centralised critic only): delay ratio and occupancy of
    every corridor of every agent plus the network summary. Agents act on
    o_i alone (centralised training, decentralised execution).
"""

from __future__ import annotations

import itertools
import json
import math
import os
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

EPOCH_S = 10.0
STEP_S = 0.5
EDGE_TIME_CAP = 300.0
SMOOTHING = 0.4
SCALE = 200.0          # vehicle-seconds per reward unit (20 vehicles for one epoch)
STRANDED_S = 600.0     # charged per vehicle still in the network at cut-off (about one full trip)


def split_table(k: int, step: float = 0.25) -> np.ndarray:
    """All splits of 1 across k corridors on a grid of `step`."""
    n = int(round(1 / step))
    rows = [c for c in itertools.product(range(n + 1), repeat=k) if sum(c) == n]
    rows.sort(key=lambda c: (-c[0], -c[1] if k > 1 else 0))
    return np.array(rows, dtype=np.float64) / n


@dataclass
class EnvConfig:
    net: Path = Path("data/net/corridor.net.xml")
    corridors: Path = Path("data/net/corridors.json")
    vehicles: int = 350
    horizon: int = 300
    tail: int = 300
    seed: int = 1
    obstacles: object = None          # path / list / None
    alpha: float = 0.5                # weight on own delay vs network delay
    epsilon: float = 0.25             # fairness tolerance
    fairness_weight: float = 0.5
    stranded_s: float = STRANDED_S
    step_length: float = STEP_S
    workdir: Path | None = None
    outputs: dict = field(default_factory=dict)   # fcd / tripinfo / summary paths, evaluation only


class CorridorCore:
    """Measurement, assignment and reward bookkeeping shared by training and evaluation."""

    def __init__(self, corridors_path: Path, horizon: float, alpha: float = 0.5,
                 epsilon: float = 0.25, fairness_weight: float = 0.5, seed: int = 0,
                 stranded_s: float = STRANDED_S) -> None:
        self.stranded_s = stranded_s
        data = json.loads(Path(corridors_path).read_text())
        self.groups = data.get("groups") or [data]
        self.n = len(self.groups)
        self.k = max(len(g["corridors"]) for g in self.groups)
        self.routes = [[tuple(c["edges"]) for c in g["corridors"]] for g in self.groups]
        self.t0 = np.zeros((self.n, self.k)); self.cap = np.ones((self.n, self.k)); self.valid = np.zeros((self.n, self.k), bool)
        for i, g in enumerate(self.groups):
            for j, c in enumerate(g["corridors"]):
                self.t0[i, j] = float(c["t0"]); self.cap[i, j] = max(1.0, float(c["capacity"])); self.valid[i, j] = True
        self.key_to_group = {(g["origin"], g["destination"]): i for i, g in enumerate(self.groups)}
        self.actions = split_table(self.k)
        self.horizon = float(horizon)
        self.alpha, self.epsilon, self.fairness_weight = alpha, epsilon, fairness_weight
        self.rng = random.Random(seed + 7919)
        self.reset_state()

    # -- dimensions ---------------------------------------------------------
    @property
    def obs_dim(self) -> int:
        return 3 * self.k + 2 + 3 + self.n

    @property
    def state_dim(self) -> int:
        return 2 * self.n * self.k + 3

    @property
    def n_actions(self) -> int:
        return len(self.actions)

    def reset_state(self) -> None:
        self.ratio = np.zeros((self.n, self.k)); self.prev_ratio = np.zeros((self.n, self.k))
        self.time = self.t0.copy(); self.occ = np.zeros((self.n, self.k))
        self.split = np.tile(self.actions[0], (self.n, 1))  # until the first decision: fastest corridor
        self.veh_group: dict[str, int] = {}
        self.active = np.zeros(self.n)
        self.schedule: list[list[float]] = [[] for _ in range(self.n)]
        self.departed_count = np.zeros(self.n)
        self.epoch_departures = np.zeros(self.n)
        self.vehsec = np.zeros(self.n)
        self.assigned = np.zeros((self.n, self.k))

    def load_schedule(self, routes_file: Path) -> None:
        """Scheduled departure times per agent, to charge vehicles waiting to enter."""
        for el in ET.parse(routes_file).getroot().iter("trip"):
            gi = self.key_to_group.get((el.get("from"), el.get("to")))
            if gi is not None:
                self.schedule[gi].append(float(el.get("depart")))
        for s in self.schedule:
            s.sort()

    # -- per simulation step ------------------------------------------------
    def on_step(self, traci, now: float, dt: float) -> None:
        for vid in traci.simulation.getDepartedIDList():
            try:
                route = traci.vehicle.getRoute(vid)
            except traci.TraCIException:
                continue
            gi = self.key_to_group.get((route[0], route[-1])) if route else None
            if gi is None:
                continue
            p = self.split[gi] * self.valid[gi]
            p = p / p.sum() if p.sum() > 0 else self.valid[gi] / self.valid[gi].sum()
            k = int(np.searchsorted(np.cumsum(p), self.rng.random() * p.sum(), side="right"))
            k = min(k, self.k - 1)
            try:
                traci.vehicle.setRoute(vid, list(self.routes[gi][k]))
            except traci.TraCIException:
                pass
            self.veh_group[vid] = gi
            self.active[gi] += 1; self.departed_count[gi] += 1; self.epoch_departures[gi] += 1
            self.assigned[gi, k] += 1
        for vid in traci.simulation.getArrivedIDList():
            gi = self.veh_group.pop(vid, None)
            if gi is not None:
                self.active[gi] -= 1
        waiting = np.array([max(0.0, np.searchsorted(s, now, side="right") - d) for s, d in zip(self.schedule, self.departed_count)])
        self.vehsec += (self.active + waiting) * dt

    # -- per epoch ----------------------------------------------------------
    def measure(self, traci) -> None:
        self.prev_ratio = self.ratio.copy()
        for i, group in enumerate(self.routes):
            for j, route in enumerate(group):
                total, t_obs = 0, 0.0
                for edge in route:
                    try:
                        total += traci.edge.getLastStepVehicleNumber(edge)
                        t_obs += min(EDGE_TIME_CAP, traci.edge.getTraveltime(edge))
                    except traci.TraCIException:
                        continue
                t_obs = max(t_obs, self.t0[i, j])
                self.time[i, j] = (1 - SMOOTHING) * self.time[i, j] + SMOOTHING * t_obs
                self.occ[i, j] = total / self.cap[i, j]
        self.ratio = np.where(self.valid, np.minimum(self.time / self.t0 - 1.0, 4.0), 0.0)

    def observations(self, now: float) -> tuple[np.ndarray, np.ndarray]:
        net_ratio = float(self.ratio[self.valid].mean())
        in_net = float(self.active.sum())
        glob = np.array([net_ratio, in_net / 500.0, min(1.5, now / self.horizon)])
        obs = np.zeros((self.n, self.obs_dim))
        for i in range(self.n):
            per = np.stack([self.ratio[i], np.minimum(self.occ[i], 3.0), np.clip(self.ratio[i] - self.prev_ratio[i], -2, 2)], 1).ravel()
            obs[i] = np.concatenate([per, [self.active[i] / 50.0, self.epoch_departures[i] / 10.0], glob, np.eye(self.n)[i]])
        state = np.concatenate([self.ratio.ravel(), np.minimum(self.occ, 3.0).ravel(), glob])
        return obs, state

    def apply(self, actions: np.ndarray) -> None:
        self.split = self.actions[actions]
        self.epoch_departures[:] = 0

    def rewards(self) -> np.ndarray:
        d = self.vehsec.copy(); self.vehsec[:] = 0
        r = -(self.alpha * d + (1 - self.alpha) * d.mean()) / SCALE
        t = np.where(self.valid, self.time, np.inf)
        excess = np.maximum(0.0, t / t.min(1, keepdims=True) - (1 + self.epsilon))
        excess = np.where(self.valid, excess, 0.0)
        r -= self.fairness_weight * (self.split * excess).sum(1)
        return r

    def stranded_penalty(self) -> np.ndarray:
        d = self.active * self.stranded_s
        return -(self.alpha * d + (1 - self.alpha) * d.mean()) / SCALE


class CorridorMARLEnv:
    """Episodic SUMO environment for the 16 corridor agents."""

    def __init__(self, cfg: EnvConfig) -> None:
        self.cfg = cfg
        self.core = CorridorCore(cfg.corridors, cfg.horizon, cfg.alpha, cfg.epsilon, cfg.fairness_weight, cfg.seed, cfg.stranded_s)
        self.workdir = Path(cfg.workdir or tempfile.mkdtemp(prefix="marl_"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self.now = 0.0

    @property
    def n_agents(self) -> int:
        return self.core.n

    def _prepare(self) -> list[str]:
        from pipeline import run_scenarios, obstacles as obstacle_model
        cfg = self.cfg
        routes = self.workdir / "demand.rou.xml"
        od = [(g["origin"], g["destination"]) for g in self.core.groups]
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            run_scenarios.generate_demand(cfg.net, routes, cfg.vehicles, cfg.horizon, cfg.seed, od=od)
        tls = self.workdir / "tls.add.xml"
        run_scenarios.write_tls_override(cfg.net, tls, "actuated")
        adds = [str(tls)]
        if cfg.outputs.get("tls_states"):
            log = self.workdir / "tls_log.add.xml"
            log.write_text(f'<additional>\n  <timedEvent type="SaveTLSStates" dest="{Path(cfg.outputs["tls_states"]).resolve()}"/>\n</additional>\n')
            adds.append(str(log))
        self.core.reset_state(); self.core.load_schedule(routes)
        obs = obstacle_model.resolve(obstacle_model.load(cfg.obstacles), cfg.net) if cfg.obstacles else []
        self.obstacles = obstacle_model.ObstacleController(obs) if obs else None
        self._obstacle_list = obs
        cmd = ["sumo", "-n", str(cfg.net), "-r", str(routes), "-a", ",".join(adds),
               "--begin", "0", "--end", str(cfg.horizon + cfg.tail), "--step-length", str(cfg.step_length),
               "--seed", str(cfg.seed), "--time-to-teleport", "300", "--ignore-route-errors",
               "--no-step-log", "--no-warnings", "--waiting-time-memory", "10000"]
        if cfg.outputs.get("fcd"):
            cmd += ["--fcd-output", str(cfg.outputs["fcd"]), "--device.fcd.period", str(cfg.outputs.get("fcd_period", 1.0))]
        if cfg.outputs.get("tripinfo"):
            cmd += ["--tripinfo-output", str(cfg.outputs["tripinfo"]), "--duration-log.statistics"]
        if cfg.outputs.get("summary"):
            cmd += ["--summary-output", str(cfg.outputs["summary"])]
        return cmd

    def reset(self) -> tuple[np.ndarray, np.ndarray]:
        import traci
        self.close()
        cmd = self._prepare()
        label = f"marl{os.getpid()}_{id(self)}"
        traci.start(cmd, label=label)
        self._conn = traci.getConnection(label)
        self._label = label
        self.now = 0.0
        self._advance(EPOCH_S)          # first epoch on the default split, so there is something to observe
        self.core.measure(self._conn)
        self.core.rewards()             # discard the warm-up epoch's cost
        return self.core.observations(self.now)

    def _advance(self, seconds: float) -> None:
        conn = self._conn
        steps = int(round(seconds / self.cfg.step_length))
        for _ in range(steps):
            conn.simulationStep()
            self.now = conn.simulation.getTime()
            if self.obstacles is not None:
                self.obstacles.step(conn, self.now)
            self.core.on_step(conn, self.now, self.cfg.step_length)
            if self._finished():
                break

    def _finished(self) -> bool:
        end = self.cfg.horizon + self.cfg.tail
        if self.now >= end - 1e-6:
            return True
        return self.now > self.cfg.horizon and self._conn.simulation.getMinExpectedNumber() == 0

    def step(self, actions: np.ndarray):
        self.core.apply(np.asarray(actions, int))
        self._advance(EPOCH_S)
        self.core.measure(self._conn)
        r = self.core.rewards()
        done = self._finished()
        if done:
            r = r + self.core.stranded_penalty()
        obs, state = self.core.observations(self.now)
        info = {"in_network": float(self.core.active.sum()), "t": self.now}
        return obs, state, r, done, info

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def assignment_summary(self) -> dict:
        tot = self.core.assigned.sum(0)
        share = (tot / max(1.0, tot.sum())).round(4).tolist()
        nz = [s for s in share if s > 0]
        ent = -sum(s * math.log(s) for s in nz) / math.log(self.core.k) if len(nz) > 1 else 0.0
        return {"assigned": int(tot.sum()), "split": tot.astype(int).tolist(), "share": share,
                "spread_entropy": round(ent, 4), "per_origin": self.core.assigned.astype(int).tolist()}
