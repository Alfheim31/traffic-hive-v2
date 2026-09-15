"""Calibrate BPR volume-delay parameters against SUMO observations.

Chapter 4 lists uncalibrated congestion parameters as a limitation: beta =
0.15 and n = 4 are United States Bureau of Public Roads defaults, untested
against Metro Manila conditions, and effective capacities were selected
rather than measured. This module removes that limitation instead of
restating it.

The procedure treats the microsimulation as the instrument and the
analytical model as the thing being measured. Demand is swept across a
corridor in isolation, realised mean travel time is recorded at each load,
and the four BPR parameters are recovered by nonlinear least squares. The
fitted model is then what drives assignment, so the analytical layer and the
simulation layer agree by construction rather than by assumption.

Usage:
    python -m pipeline.calibrate --sweep 50 100 200 400 800 --horizon 600
    python -m pipeline.calibrate --fit-only data/out/calibration.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from scipy.optimize import curve_fit

from pipeline import run_scenarios
from pipeline.run_scenarios import SCENARIOS

NET = Path("data/net/corridor.net.xml")
OUT = Path("data/out/calibration")


@dataclass
class BPRFit:
    """Fitted volume-delay parameters with goodness-of-fit."""

    t0: float
    capacity: float
    beta: float
    n: float
    r_squared: float
    rmse: float
    n_points: int

    def to_dict(self) -> dict:
        return {
            "t0": round(self.t0, 4),
            "capacity": round(self.capacity, 4),
            "beta": round(self.beta, 6),
            "n": round(self.n, 4),
            "r_squared": round(self.r_squared, 6),
            "rmse": round(self.rmse, 4),
            "n_points": self.n_points,
        }


def bpr(load: np.ndarray, t0: float, capacity: float, beta: float, n: float) -> np.ndarray:
    """Volume-delay function in the form fitted to observations.

    T(N) = T0 [ 1 + beta (N / C)^n ]

    All four parameters are free. Fixing n at 4 and fitting only the rest is
    also defensible and more stable on sparse data; see `fit_bpr(fix_n=...)`.
    """
    return t0 * (1.0 + beta * np.power(np.maximum(load, 0.0) / capacity, n))


def fit_bpr(
    loads: np.ndarray,
    times: np.ndarray,
    fix_n: float | None = None,
) -> BPRFit:
    """Recover BPR parameters by nonlinear least squares.

    Initial guesses matter for convergence here: T0 is seeded from the
    fastest observation (the least congested point approximates free flow),
    capacity from the median load, and beta and n from the BPR defaults. A
    poor seed on a four-parameter power law can converge to a local minimum
    that fits the data but has no physical interpretation.

    Bounds keep the result interpretable: all parameters strictly positive,
    and the exponent confined to a range consistent with the published
    volume-delay literature.
    """
    loads = np.asarray(loads, dtype=float)
    times = np.asarray(times, dtype=float)
    if loads.size < 4:
        raise ValueError(
            f"Need at least 4 observations to fit 4 parameters; got {loads.size}."
        )

    t0_seed = float(np.min(times))
    cap_seed = max(float(np.median(loads)), 1.0)

    if fix_n is None:
        p0 = [t0_seed, cap_seed, 0.15, 4.0]
        bounds = ([1e-3, 1e-3, 1e-4, 1.0], [np.inf, np.inf, 10.0, 12.0])
        model = bpr
    else:
        p0 = [t0_seed, cap_seed, 0.15]
        bounds = ([1e-3, 1e-3, 1e-4], [np.inf, np.inf, 10.0])

        def model(load, t0, capacity, beta):  # type: ignore[misc]
            return bpr(load, t0, capacity, beta, fix_n)

    popt, _ = curve_fit(model, loads, times, p0=p0, bounds=bounds, maxfev=20000)

    if fix_n is None:
        t0, capacity, beta, n = popt
    else:
        t0, capacity, beta = popt
        n = fix_n

    predicted = bpr(loads, t0, capacity, beta, n)
    residual = times - predicted
    ss_res = float(np.sum(residual**2))
    ss_tot = float(np.sum((times - np.mean(times)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rmse = float(np.sqrt(np.mean(residual**2)))

    return BPRFit(
        t0=float(t0),
        capacity=float(capacity),
        beta=float(beta),
        n=float(n),
        r_squared=r2,
        rmse=rmse,
        n_points=int(loads.size),
    )


def observe(tripinfo_path: Path) -> tuple[float, float, int]:
    """Mean travel time, mean route length and trip count from one run."""
    durations: list[float] = []
    lengths: list[float] = []
    for _, elem in ET.iterparse(str(tripinfo_path), events=("end",)):
        if elem.tag != "tripinfo":
            continue
        durations.append(float(elem.get("duration", 0)))
        lengths.append(float(elem.get("routeLength", 0)))
        elem.clear()
    if not durations:
        return 0.0, 0.0, 0
    return float(np.mean(durations)), float(np.mean(lengths)), len(durations)


def sweep(
    demands: list[int],
    horizon: int,
    seed: int,
    net_path: Path = NET,
    scenario_name: str = "actuated",
) -> dict:
    """Run the network at each demand level and record realised travel time.

    Uses the shortest-path scenario deliberately. Calibration should measure
    how the network responds to load, not how a routing policy redistributes
    it, so the policy under study must not be active while the instrument is
    being calibrated.
    """
    scenario = SCENARIOS[scenario_name]
    observations = []

    for demand in demands:
        print(f"  sweeping demand={demand}")
        paths = run_scenarios.run(
            scenario,
            n_vehicles=demand,
            horizon=horizon,
            seed=seed,
            net_path=net_path,
        )
        mean_time, mean_length, completed = observe(paths["tripinfo"])
        observations.append({
            "demand": demand,
            "completed": completed,
            "mean_travel_time_s": round(mean_time, 4),
            "mean_route_length_m": round(mean_length, 2),
        })
        print(f"    mean travel time {mean_time:.1f}s over {completed} trips")

    return {"scenario": scenario_name, "horizon": horizon,
            "seed": seed, "observations": observations}


def calibrate(sweep_data: dict, fix_n: float | None = None) -> dict:
    """Fit the volume-delay function to a completed sweep."""
    obs = [o for o in sweep_data["observations"] if o["completed"] > 0]
    loads = np.array([o["demand"] for o in obs], dtype=float)
    times = np.array([o["mean_travel_time_s"] for o in obs], dtype=float)

    free_fit = fit_bpr(loads, times, fix_n=None)
    fixed_fit = fit_bpr(loads, times, fix_n=4.0)

    # The default parameterisation, evaluated on the same data, so the
    # comparison that justifies calibrating at all is explicit.
    default_pred = bpr(loads, float(np.min(times)), float(np.median(loads)), 0.15, 4.0)
    default_rmse = float(np.sqrt(np.mean((times - default_pred) ** 2)))

    return {
        **sweep_data,
        "fit_free_exponent": free_fit.to_dict(),
        "fit_fixed_exponent_4": fixed_fit.to_dict(),
        "bpr_default_rmse": round(default_rmse, 4),
        "improvement_over_default_pct": round(
            (default_rmse - free_fit.rmse) / default_rmse * 100.0, 2
        )
        if default_rmse > 0
        else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", nargs="+", type=int,
                        default=[100, 200, 400, 700, 1000, 1500])
    parser.add_argument("--horizon", type=int, default=600)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--net", type=Path, default=NET)
    parser.add_argument("--fit-only", type=Path, default=None,
                        help="Skip simulation and fit an existing sweep file.")
    parser.add_argument("--out", type=Path,
                        default=Path("data/out/calibration.json"))
    args = parser.parse_args()

    if args.fit_only:
        sweep_data = json.loads(args.fit_only.read_text())
    else:
        print("running demand sweep for calibration...")
        sweep_data = sweep(args.sweep, args.horizon, args.seed, args.net)

    result = calibrate(sweep_data)

    free = result["fit_free_exponent"]
    fixed = result["fit_fixed_exponent_4"]
    print("\nFitted volume-delay parameters")
    print("-" * 46)
    print(f"{'':<14}{'free n':>14}{'n fixed at 4':>16}")
    print(f"{'T0 (s)':<14}{free['t0']:>14.3f}{fixed['t0']:>16.3f}")
    print(f"{'capacity':<14}{free['capacity']:>14.3f}{fixed['capacity']:>16.3f}")
    print(f"{'beta':<14}{free['beta']:>14.4f}{fixed['beta']:>16.4f}")
    print(f"{'n':<14}{free['n']:>14.3f}{fixed['n']:>16.3f}")
    print(f"{'R squared':<14}{free['r_squared']:>14.4f}{fixed['r_squared']:>16.4f}")
    print(f"{'RMSE (s)':<14}{free['rmse']:>14.3f}{fixed['rmse']:>16.3f}")
    print("-" * 46)
    print(f"BPR defaults RMSE: {result['bpr_default_rmse']:.3f} s")
    print(f"Improvement from calibration: "
          f"{result['improvement_over_default_pct']:.1f}%")

    if free["beta"] > 0.5:
        print("\nNote: fitted beta substantially exceeds the 0.15 default, "
              "indicating this network congests faster than the BPR standard "
              "assumes. Worth reporting explicitly.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
