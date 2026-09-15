"""Generate publication figures from pipeline outputs.

Every figure regenerates from data files, so re-running an experiment
refreshes the plots rather than requiring them to be re-exported by hand.
That property matters more than it sounds: figures that are produced
manually drift out of step with the numbers in the text, and the drift is
invisible until someone checks.

Figures produced:

  fig1_algorithm_comparison   measured SUMO outcomes per routing rule
  fig2_poa_vs_demand          Price of Anarchy across demand, with the
                              regime boundary where coordination can help
  fig3_lambda_sweep           SSO collapsing to concentration as lambda grows
  fig4_epsilon_sweep          the fairness-versus-efficiency trade-off
  fig5_calibration            fitted volume-delay function against observed
  fig6_queue_timeseries       queue evolution per rule

Usage:
    python -m pipeline.figures --all
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # no display in a headless environment
import matplotlib.pyplot as plt
import numpy as np

from pipeline import theory

FIGDIR = Path("figures")
METRICS = Path("app/assets/sim/metrics.json")
CALIBRATION = Path("data/out/calibration.json")

# A restrained palette: the proposed method is the only saturated colour, so
# the eye goes to it without the figure relying on colour alone to carry
# meaning. Baselines are distinguished by hatch and marker as well.
C_PROPOSED = "#1D6E56"
C_BASELINE = "#8C9080"
C_ALT = "#B4623A"
C_GRID = "#DDDDD8"

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 10,
    "axes.edgecolor": "#444444",
    "axes.linewidth": 0.8,
    "axes.grid": True,
    "grid.color": C_GRID,
    "grid.linewidth": 0.6,
    "figure.dpi": 150,
    "savefig.bbox": "tight",
})


def _save(fig, name: str) -> Path:
    FIGDIR.mkdir(parents=True, exist_ok=True)
    path = FIGDIR / f"{name}.png"
    fig.savefig(path)
    pdf = FIGDIR / f"{name}.pdf"
    fig.savefig(pdf)  # vector copy for the manuscript
    plt.close(fig)
    print(f"  wrote {path} and {pdf.name}")
    return path


# ---------------------------------------------------------------------------
# Figure 1 — measured comparison
# ---------------------------------------------------------------------------

def fig_algorithm_comparison(metrics: dict) -> None:
    """Measured outcomes per routing rule, from the SUMO runs."""
    labels_map = metrics.get("labels", {})
    names = list(metrics["metrics"].keys())
    labels = [labels_map.get(n, n) for n in names]

    time_loss = [metrics["metrics"][n]["avg_time_loss_s"] for n in names]
    waiting = [metrics["metrics"][n]["avg_waiting_s"] for n in names]
    peak_q = [metrics["metrics"][n]["peak_halting"] for n in names]

    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.4))
    panels = [
        (axes[0], time_loss, "Mean time loss (s)"),
        (axes[1], waiting, "Mean waiting time (s)"),
        (axes[2], peak_q, "Peak queued vehicles"),
    ]
    for ax, values, title in panels:
        colours = [C_PROPOSED if "hive" in n and "sue" not in n else C_BASELINE
                   for n in names]
        bars = ax.bar(range(len(values)), values, color=colours, width=0.62)
        for bar, v in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, v,
                    f"{v:.0f}", ha="center", va="bottom", fontsize=8)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
        ax.set_title(title, fontsize=10)
        ax.set_axisbelow(True)
        ax.margins(y=0.16)

    params = metrics.get("params", {})
    fig.suptitle(
        f"Measured routing outcomes — {params.get('vehicles', '?')} vehicles, "
        f"{params.get('duration_s', '?')} s, seed {params.get('seed', '?')}",
        fontsize=11, y=1.04,
    )
    _save(fig, "fig1_algorithm_comparison")


# ---------------------------------------------------------------------------
# Figure 2 — Price of Anarchy
# ---------------------------------------------------------------------------

def fig_poa(corridors: theory.CorridorSet, demands: list[int]) -> None:
    """Price of Anarchy against demand.

    The most important figure in the set: it bounds what any coordination
    mechanism can achieve at each demand level, and so converts an
    unqualified performance claim into a scoped one.
    """
    curve = theory.poa_curve(corridors, demands)
    d = [c["demand"] for c in curve]
    poa = [c["poa"] for c in curve]

    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    ax.plot(d, poa, marker="o", markersize=4, color=C_PROPOSED, linewidth=1.6)
    ax.axhline(1.0, color=C_BASELINE, linestyle="--", linewidth=1.0)
    ax.text(d[0], 1.001, "selfish routing already optimal",
            fontsize=8, color="#666666", va="bottom")

    peak = int(np.argmax(poa))
    ax.annotate(
        f"max PoA {poa[peak]:.4f}\nat demand {d[peak]}",
        xy=(d[peak], poa[peak]),
        xytext=(0.62, 0.26), textcoords="axes fraction", fontsize=8,
        arrowprops=dict(arrowstyle="->", color="#666666", linewidth=0.8),
    )

    ax.set_xlabel("Demand (vehicles)")
    ax.set_ylabel("Price of Anarchy  (UE cost / SO cost)")
    ax.set_title("Available improvement from coordination, by demand", fontsize=11)
    ax.set_axisbelow(True)
    _save(fig, "fig2_poa_vs_demand")


# ---------------------------------------------------------------------------
# Figure 3 — lambda sweep
# ---------------------------------------------------------------------------

def fig_lambda(corridors: theory.CorridorSet, demand: int) -> None:
    """Synchronized System Optimum collapsing to concentration.

    Evidence that the failure of the variance penalty is structural rather
    than a tuning artefact: there is no value of lambda at which the term
    both binds and distributes demand.
    """
    lambdas = [0.0, 0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500]
    rows = theory.lambda_sweep(corridors, demand, lambdas)

    used = [int(np.count_nonzero(r["load"])) for r in rows]
    cost = [r["total_cost"] for r in rows]
    so = theory.solve_system_optimum(corridors, demand)

    fig, ax = plt.subplots(figsize=(6.6, 3.8))
    ax.semilogx(
        [max(l, 1e-2) for l in lambdas], cost,
        marker="o", markersize=4, color=C_ALT, linewidth=1.5,
        label="SSO total cost",
    )
    ax.axhline(so.total_cost, color=C_PROPOSED, linestyle="--", linewidth=1.2,
               label=f"System Optimum ({so.total_cost:.1f})")
    ax.set_xlabel("lambda  (weight on arrival-time variance)")
    ax.set_ylabel("Total network travel time")

    ax2 = ax.twinx()
    ax2.semilogx([max(l, 1e-2) for l in lambdas], used,
                 color=C_BASELINE, linewidth=1.0, linestyle=":", marker="s",
                 markersize=3, label="corridors used")
    ax2.set_ylabel("Corridors carrying demand")
    ax2.set_ylim(0, corridors.k + 0.5)
    ax2.grid(False)

    lines = ax.get_lines() + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], fontsize=8, loc="center left")
    ax.set_title("No value of lambda both binds and distributes demand", fontsize=11)
    _save(fig, "fig3_lambda_sweep")


# ---------------------------------------------------------------------------
# Figure 4 — epsilon sweep
# ---------------------------------------------------------------------------

def fig_epsilon(corridors: theory.CorridorSet, demand: int) -> None:
    """Fairness tolerance against efficiency cost.

    Establishes epsilon as a designed parameter rather than an arbitrary
    one, by making the price of each fairness level explicit and showing
    where the constraint stops binding.
    """
    eps = [round(e, 3) for e in np.arange(0.0, 1.01, 0.05)]
    rows = theory.epsilon_sweep(corridors, demand, eps)

    penalty = [r["cost_penalty_pct"] for r in rows]
    spread = [r["spread"] for r in rows]
    binding = [r["binding"] for r in rows]

    fig, ax = plt.subplots(figsize=(6.6, 3.8))
    ax.plot(eps, penalty, marker="o", markersize=3.5,
            color=C_PROPOSED, linewidth=1.6, label="cost above System Optimum (%)")
    ax.set_xlabel("epsilon  (fairness tolerance)")
    ax.set_ylabel("Efficiency cost (% above SO)")

    ax2 = ax.twinx()
    ax2.plot(eps, spread, color=C_ALT, linewidth=1.3, linestyle="--",
             label="worst-case travel-time spread")
    ax2.set_ylabel("Spread between used corridors")
    ax2.grid(False)

    # Mark where the constraint stops binding.
    slack = next((e for e, b in zip(eps, binding) if not b), None)
    if slack is not None:
        ax.axvline(slack, color=C_BASELINE, linestyle=":", linewidth=1.0)
        ax.text(slack, max(penalty) * 0.7 if max(penalty) else 0.1,
                f"  constraint slack\n  beyond {slack:g}",
                fontsize=8, color="#666666")

    lines = ax.get_lines()[:1] + ax2.get_lines()
    ax.legend(lines, [l.get_label() for l in lines], fontsize=8)
    ax.set_title("Cost of fairness across the tolerance range", fontsize=11)
    _save(fig, "fig4_epsilon_sweep")


# ---------------------------------------------------------------------------
# Figure 5 — calibration
# ---------------------------------------------------------------------------

def fig_calibration(cal: dict) -> None:
    """Fitted volume-delay function against SUMO observations."""
    obs = [o for o in cal["observations"] if o["completed"] > 0]
    loads = np.array([o["demand"] for o in obs], dtype=float)
    times = np.array([o["mean_travel_time_s"] for o in obs], dtype=float)

    free = cal["fit_free_exponent"]
    fixed = cal["fit_fixed_exponent_4"]
    grid = np.linspace(loads.min(), loads.max(), 240)

    from pipeline.calibrate import bpr

    fig, ax = plt.subplots(figsize=(6.6, 3.9))
    ax.scatter(loads, times, s=34, color="#333333", zorder=3,
               label="SUMO observations")
    ax.plot(grid, bpr(grid, free["t0"], free["capacity"], free["beta"], free["n"]),
            color=C_PROPOSED, linewidth=1.8,
            label=f"fitted  n={free['n']:.2f}, beta={free['beta']:.3f}  "
                  f"(R²={free['r_squared']:.4f})")
    ax.plot(grid, bpr(grid, fixed["t0"], fixed["capacity"], fixed["beta"], 4.0),
            color=C_BASELINE, linewidth=1.4, linestyle="--",
            label=f"BPR default n=4  (R²={fixed['r_squared']:.4f})")

    ax.set_xlabel("Demand (vehicles)")
    ax.set_ylabel("Mean travel time (s)")
    ax.set_title("Volume-delay function calibrated against microsimulation",
                 fontsize=11)
    ax.legend(fontsize=8, loc="upper left")
    ax.set_axisbelow(True)
    _save(fig, "fig5_calibration")


# ---------------------------------------------------------------------------
# Figure 6 — queue time series
# ---------------------------------------------------------------------------

def fig_queues(metrics: dict) -> None:
    """Queue evolution over the run, per routing rule."""
    labels_map = metrics.get("labels", {})
    fig, ax = plt.subplots(figsize=(7.0, 3.6))

    for name, m in metrics["metrics"].items():
        series = m.get("series", {})
        t = series.get("t", [])
        halting = series.get("halting", [])
        if not t:
            continue
        proposed = "hive" in name and "sue" not in name
        ax.plot(t, halting,
                color=C_PROPOSED if proposed else C_BASELINE,
                linewidth=1.8 if proposed else 1.1,
                linestyle="-" if proposed else "--",
                label=labels_map.get(name, name))

    ax.set_xlabel("Simulation time (s)")
    ax.set_ylabel("Vehicles queued")
    ax.set_title("Queue formation over the run", fontsize=11)
    ax.legend(fontsize=8)
    ax.set_axisbelow(True)
    _save(fig, "fig6_queue_timeseries")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, default=METRICS)
    parser.add_argument("--calibration", type=Path, default=CALIBRATION)
    parser.add_argument("--demand", type=int, default=10)
    parser.add_argument("--t0", nargs="+", type=float, default=None)
    parser.add_argument("--capacity", nargs="+", type=float, default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    corridors = theory.default_corridors()
    if args.t0:
        corridors.t0 = np.array(args.t0, dtype=float)
    if args.capacity:
        corridors.capacity = np.array(args.capacity, dtype=float)
    if len(corridors.names) != len(corridors.t0):
        corridors.names = [f"Corridor {i+1}" for i in range(len(corridors.t0))]

    print("generating figures...")

    if args.metrics.exists():
        metrics = json.loads(args.metrics.read_text())
        fig_algorithm_comparison(metrics)
        fig_queues(metrics)
    else:
        print(f"  skipping measured figures: {args.metrics} not found")

    fig_poa(corridors, list(range(2, 41, 2)))
    fig_lambda(corridors, args.demand)
    fig_epsilon(corridors, args.demand)

    if args.calibration.exists():
        fig_calibration(json.loads(args.calibration.read_text()))
    else:
        print(f"  skipping calibration figure: {args.calibration} not found")

    print(f"\nfigures written to {FIGDIR}/")


if __name__ == "__main__":
    main()
