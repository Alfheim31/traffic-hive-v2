"""Analytical corridor model: UE, SO, SSO and the constrained optimum.

Reproduces the exhaustive enumeration of Chapter 4 in code, so every figure
and every number quoted in the paper can be regenerated from one command
rather than transcribed by hand.

Four allocation rules are computed over the same corridor set:

UE      User Equilibrium. Each vehicle minimises its own travel time, so at
        equilibrium every used corridor exhibits equal travel time. This is
        what selfish routing — and any navigation app — converges to.

SO      System Optimum. Minimises total network travel time. Reached by
        assigning on marginal social cost rather than travel time.

SSO     Synchronized System Optimum. SO plus a lambda-weighted penalty on
        arrival-time variance. Retained because demonstrating its failure is
        a result: the variance term has a degenerate minimiser, since
        concentrating all demand on one corridor yields identical — and
        uniformly poor — travel times, hence zero variance.

CSO     Constrained System Optimum. Minimises total travel time subject to no
        used corridor exceeding (1 + epsilon) times the fastest used
        corridor. Unlike a penalty term this cannot be gamed by
        concentration: a concentrated allocation violates the constraint and
        leaves the feasible set entirely.

Usage:
    python -m pipeline.theory --demand 10 --report
"""

from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

# Capacities follow the corridor configuration evaluated in Chapter 4.
#
# PLACEHOLDER: the free-flow times below are illustrative, not the values
# from the manuscript. Override them with --t0, or fit them from SUMO with
# pipeline.calibrate, before quoting any figure produced here. With these
# placeholders the Price of Anarchy is 1.0461; the manuscript reports 1.0888,
# and the difference is entirely attributable to T0.
DEFAULT_T0 = np.array([12.0, 15.0, 20.0])
DEFAULT_CAPACITY = np.array([10.0, 8.0, 7.0])
DEFAULT_NAMES = ["EDSA", "C-5", "Skyway Stage 3"]
BETA = 0.15
BPR_N = 4


@dataclass
class CorridorSet:
    """Free-flow times, capacities and labels for K corridors."""

    t0: np.ndarray
    capacity: np.ndarray
    names: list[str]
    beta: float = BETA
    n: int = BPR_N

    @property
    def k(self) -> int:
        return len(self.t0)

    def travel_time(self, load: np.ndarray) -> np.ndarray:
        """BPR volume-delay function, evaluated per corridor.

        T_k(N) = T0_k [ 1 + beta (N / C_k)^n ]
        """
        ratio = np.asarray(load, dtype=float) / self.capacity
        return self.t0 * (1.0 + self.beta * ratio**self.n)

    def marginal_cost(self, load: np.ndarray) -> np.ndarray:
        """Marginal social cost of an additional vehicle on each corridor.

        Differentiating total corridor cost Z(N) = N T(N) gives

            MC_k(N) = T0_k [ 1 + (n + 1) beta (N / C_k)^n ]

        The congestion coefficient is multiplied by (n + 1) — with n = 4,
        beta rises from 0.15 to 0.75. That surplus is the externality each
        additional vehicle imposes on those already present, and ignoring it
        is precisely what makes User Equilibrium inefficient.
        """
        ratio = np.asarray(load, dtype=float) / self.capacity
        return self.t0 * (1.0 + (self.n + 1) * self.beta * ratio**self.n)

    def total_cost(self, load: np.ndarray) -> float:
        """Total vehicle-time across the network for an allocation."""
        load = np.asarray(load, dtype=float)
        return float(np.sum(load * self.travel_time(load)))


@dataclass
class Allocation:
    """One evaluated allocation of demand across corridors."""

    load: list[int]
    total_cost: float
    mean_time: float
    times: list[float]
    variance: float
    spread: float
    rule: str

    def to_dict(self) -> dict:
        return asdict(self)


def enumerate_allocations(demand: int, k: int) -> np.ndarray:
    """Every integer allocation of `demand` vehicles across `k` corridors.

    The count is C(demand + k - 1, k - 1) — 66 for ten vehicles across three
    corridors, which is small enough that exhaustive enumeration is exact
    and no optimiser is needed. Exactness matters here: the claim being made
    is that a global optimum has a particular form, and a search that could
    miss it would not support that claim.
    """
    rows = []
    for combo in itertools.combinations_with_replacement(range(k), demand):
        counts = np.bincount(combo, minlength=k)
        rows.append(counts)
    unique = {tuple(int(v) for v in r) for r in rows}
    return np.array(sorted(unique), dtype=int)


def _describe(corridors: CorridorSet, load: np.ndarray, rule: str) -> Allocation:
    load = np.asarray(load, dtype=int)
    times = corridors.travel_time(load.astype(float))
    used = load > 0
    used_times = times[used] if used.any() else times
    return Allocation(
        load=[int(v) for v in load],
        total_cost=round(corridors.total_cost(load.astype(float)), 4),
        mean_time=round(float(np.average(times, weights=np.maximum(load, 1e-9))), 4),
        times=[round(float(t), 4) for t in times],
        variance=round(float(np.var(used_times)), 6),
        spread=round(float(used_times.max() - used_times.min()), 4),
        rule=rule,
    )


def solve_system_optimum(corridors: CorridorSet, demand: int) -> Allocation:
    """Allocation minimising total network travel time."""
    grid = enumerate_allocations(demand, corridors.k)
    costs = np.array([corridors.total_cost(row.astype(float)) for row in grid])
    return _describe(corridors, grid[int(np.argmin(costs))], "SO")


def solve_user_equilibrium(corridors: CorridorSet, demand: int) -> Allocation:
    """Allocation reached by selfish routing.

    Assigns one vehicle at a time to whichever corridor currently offers the
    lowest travel time. This incremental construction converges to the
    Wardrop first-principle equilibrium for separable, monotonically
    increasing cost functions, which the BPR form satisfies.
    """
    load = np.zeros(corridors.k, dtype=int)
    for _ in range(demand):
        trial = corridors.travel_time((load + np.eye(corridors.k, dtype=int)).astype(float))
        own = np.diag(trial)
        load[int(np.argmin(own))] += 1
    return _describe(corridors, load, "UE")


def solve_synchronized(
    corridors: CorridorSet, demand: int, lam: float
) -> Allocation:
    """Minimise total cost plus lambda times arrival-time variance.

    Included to demonstrate its failure rather than to advocate it. As
    lambda grows the variance term dominates, and because a concentrated
    allocation has exactly zero variance among used corridors, the minimiser
    collapses onto concentration — the opposite of distributing demand.
    """
    grid = enumerate_allocations(demand, corridors.k)
    scores = []
    for row in grid:
        times = corridors.travel_time(row.astype(float))
        used = row > 0
        var = float(np.var(times[used])) if used.any() else 0.0
        scores.append(corridors.total_cost(row.astype(float)) + lam * var)
    return _describe(corridors, grid[int(np.argmin(scores))], f"SSO(lambda={lam:g})")


def solve_constrained(
    corridors: CorridorSet, demand: int, epsilon: float
) -> Allocation:
    """Minimise total cost subject to a bounded fairness ratio.

    Feasible allocations are those where every used corridor satisfies
    T_k <= (1 + epsilon) min_j T_j over used corridors. Concentration is
    excluded outright rather than merely disfavoured, which is what
    distinguishes a constraint from a penalty term.

    Falls back to the unconstrained optimum only if no allocation is
    feasible, which happens for very small epsilon.
    """
    grid = enumerate_allocations(demand, corridors.k)
    best = None
    best_cost = np.inf
    for row in grid:
        times = corridors.travel_time(row.astype(float))
        used = row > 0
        if not used.any():
            continue
        used_times = times[used]
        if used_times.max() > (1.0 + epsilon) * used_times.min():
            continue
        cost = corridors.total_cost(row.astype(float))
        if cost < best_cost:
            best_cost = cost
            best = row
    if best is None:
        return solve_system_optimum(corridors, demand)
    return _describe(corridors, best, f"CSO(eps={epsilon:g})")


def price_of_anarchy(corridors: CorridorSet, demand: int) -> float:
    """Ratio of User Equilibrium cost to System Optimum cost.

    A value of 1.0 means selfish routing is already optimal and no
    coordination mechanism can help. Larger values bound the improvement
    available, so this quantity defines the regime in which the proposed
    method can possibly be useful.
    """
    so = solve_system_optimum(corridors, demand)
    ue = solve_user_equilibrium(corridors, demand)
    if so.total_cost <= 0:
        return 1.0
    return float(ue.total_cost / so.total_cost)


def default_corridors() -> CorridorSet:
    return CorridorSet(
        t0=DEFAULT_T0.copy(),
        capacity=DEFAULT_CAPACITY.copy(),
        names=list(DEFAULT_NAMES),
    )


def summarise(corridors: CorridorSet, demand: int, epsilon: float = 0.25) -> dict:
    """Every analytical quantity the app and the figures need."""
    so = solve_system_optimum(corridors, demand)
    ue = solve_user_equilibrium(corridors, demand)
    cso = solve_constrained(corridors, demand, epsilon)
    sso = solve_synchronized(corridors, demand, lam=1.0)

    return {
        "demand": demand,
        "corridors": {
            "names": corridors.names,
            "t0": [float(v) for v in corridors.t0],
            "capacity": [float(v) for v in corridors.capacity],
            "beta": corridors.beta,
            "n": corridors.n,
        },
        "allocations": {
            "UE": ue.to_dict(),
            "SO": so.to_dict(),
            "CSO": cso.to_dict(),
            "SSO": sso.to_dict(),
        },
        "price_of_anarchy": round(price_of_anarchy(corridors, demand), 6),
        "so_improvement_pct": round(
            (ue.total_cost - so.total_cost) / ue.total_cost * 100.0, 4
        )
        if ue.total_cost
        else 0.0,
        "cso_improvement_pct": round(
            (ue.total_cost - cso.total_cost) / ue.total_cost * 100.0, 4
        )
        if ue.total_cost
        else 0.0,
        "epsilon": epsilon,
    }


def lambda_sweep(
    corridors: CorridorSet, demand: int, lambdas: list[float]
) -> list[dict]:
    """SSO allocation across lambda, to show the collapse to concentration."""
    out = []
    for lam in lambdas:
        alloc = solve_synchronized(corridors, demand, lam)
        concentrated = int(np.count_nonzero(alloc.load) == 1)
        out.append({
            "lambda": lam,
            "load": alloc.load,
            "total_cost": alloc.total_cost,
            "variance": alloc.variance,
            "concentrated": concentrated,
        })
    return out


def epsilon_sweep(
    corridors: CorridorSet, demand: int, epsilons: list[float]
) -> list[dict]:
    """Constrained optimum across epsilon, trading total cost against spread.

    This is the evidence that epsilon is a designed parameter rather than an
    arbitrary one: it shows the cost of fairness explicitly, and identifies
    the value beyond which the constraint stops binding and the solution
    reverts to the unconstrained optimum.
    """
    so = solve_system_optimum(corridors, demand)
    out = []
    for eps in epsilons:
        alloc = solve_constrained(corridors, demand, eps)
        out.append({
            "epsilon": eps,
            "load": alloc.load,
            "total_cost": alloc.total_cost,
            "spread": alloc.spread,
            "cost_penalty_pct": round(
                (alloc.total_cost - so.total_cost) / so.total_cost * 100.0, 4
            )
            if so.total_cost
            else 0.0,
            "binding": int(alloc.load != so.load),
        })
    return out


def poa_curve(corridors: CorridorSet, demands: list[int]) -> list[dict]:
    """Price of Anarchy against demand.

    Identifies the saturation regime in which coordination pays. Below the
    knee, selfish routing is near-optimal and no method can claim much; above
    it, the available improvement grows. Reporting this converts an
    unqualified claim into a scoped one.
    """
    out = []
    for d in demands:
        so = solve_system_optimum(corridors, d)
        ue = solve_user_equilibrium(corridors, d)
        out.append({
            "demand": d,
            "poa": round(ue.total_cost / so.total_cost, 6) if so.total_cost else 1.0,
            "ue_cost": ue.total_cost,
            "so_cost": so.total_cost,
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demand", type=int, default=10)
    parser.add_argument(
        "--t0", nargs="+", type=float, default=None,
        help="Free-flow travel times per corridor, overriding the placeholders.",
    )
    parser.add_argument(
        "--capacity", nargs="+", type=float, default=None,
        help="Effective capacities per corridor.",
    )
    parser.add_argument("--epsilon", type=float, default=0.25)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    corridors = default_corridors()
    if args.t0:
        corridors.t0 = np.array(args.t0, dtype=float)
    if args.capacity:
        corridors.capacity = np.array(args.capacity, dtype=float)
    if len(corridors.names) != len(corridors.t0):
        corridors.names = [f"Corridor {i + 1}" for i in range(len(corridors.t0))]
    summary = summarise(corridors, args.demand, args.epsilon)

    if args.report:
        print(f"Corridors: {', '.join(corridors.names)}")
        print(f"Free-flow: {[float(v) for v in corridors.t0]}")
        print(f"Capacity:  {[float(v) for v in corridors.capacity]}")
        print(f"Demand:    {args.demand}\n")
        for rule, alloc in summary["allocations"].items():
            print(
                f"{rule:<16} load={alloc['load']}  "
                f"cost={alloc['total_cost']:.3f}  "
                f"spread={alloc['spread']:.3f}"
            )
        print(f"\nPrice of Anarchy: {summary['price_of_anarchy']:.4f}")
        print(f"SO improvement over UE:  {summary['so_improvement_pct']:.2f}%")
        print(f"CSO improvement over UE: {summary['cso_improvement_pct']:.2f}%")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
