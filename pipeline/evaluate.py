"""Replicated evaluation of the Hive corridor rule against selfish routing.

Runs `hive_corridor` and `ue_corridor` on identical demand for several
seeds, optionally with roadside obstacles, and reports:

  vehicle_hours         total time of ALL vehicles in, or waiting to enter,
                        the network over the run, finished or not; the
                        primary measure, free of survivorship bias
  unfinished            vehicles still in the network when the run ended
  completion            share of inserted trips that finished
  time_loss, waiting    per completed trip (s)
  peak_halting          worst network-wide queue (vehicles)
  p90_dur               90th percentile trip duration (s)
  edges_used            road segments that carried any traffic
  gini_edge_load        inequality of vehicle-seconds across segments
                        (0 = perfectly even, 1 = all on one segment)
  top10pct_edge_share   share of all vehicle-seconds on the busiest 10%
                        of segments
  entropy               normalised entropy of the corridor split
                        (1 = demand divided evenly across alternatives)
  win_rate              share of seeds where a rule's total vehicle-hours
                        are below the baseline's
  paired_faster         share of vehicles whose own trip was faster under
                        Hive than under UE (same id = same OD and departure)

The last metric will not approach 100% under any system-optimal rule: some
drivers are sent on a slightly longer route so that everyone else saves
more. The fairness tolerance epsilon bounds how much longer.

    python -m pipeline.evaluate --vehicles 1000 --seeds 11 22 33 44 55
    python -m pipeline.evaluate --vehicles 1000 --obstacles examples/obstacles_espana_demo.json
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import io
import json
import statistics as st
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

from pipeline import pack

ROOT = Path("data/eval")
PAIR = ("ue_corridor", "hive_corridor")


def network_spread(run_dir: Path) -> dict:
    """How evenly traffic used the road network, from the FCD output."""
    load: collections.Counter[str] = collections.Counter()
    for _, el in ET.iterparse(run_dir / "fcd.xml"):
        if el.tag == "vehicle":
            lane = el.get("lane", "")
            if lane and not lane.startswith(":"):
                load[lane.rsplit("_", 1)[0]] += 1
        elif el.tag == "timestep":
            el.clear()
    vals = sorted(load.values())
    n, total = len(vals), sum(vals) or 1
    gini = (2 * sum((i + 1) * v for i, v in enumerate(vals))) / (n * total) - (n + 1) / n if n else 0.0
    top = vals[-max(1, n // 10):] if n else []
    return {"edges_used": n, "gini_edge_load": round(gini, 4),
            "top10pct_edge_share": round(sum(top) / total, 4)}


def system_totals(run_dir: Path) -> dict:
    """Totals over ALL vehicles, finished or not, from SUMO's summary output.

    Per-trip averages are taken over completed trips only, so a rule that
    strands more vehicles can look better on them than it is. Total
    vehicle-hours (vehicles in the network plus those waiting to be
    inserted, summed over the whole run) has no such bias, and is the
    quantity a system-optimal assignment minimises.
    """
    steps = list(ET.parse(run_dir / "summary.xml").getroot().iter("step"))
    if len(steps) < 2:
        return {"vehicle_hours": None, "unfinished": None}
    dt = float(steps[1].get("time")) - float(steps[0].get("time"))
    vh = sum(float(s.get("running")) + float(s.get("waiting")) for s in steps) * dt / 3600.0
    last = steps[-1]
    return {"vehicle_hours": round(vh, 2), "unfinished": int(last.get("running")) + int(last.get("waiting"))}


def summarise(run_dir: Path) -> dict:
    m = pack.compute_metrics(run_dir / "tripinfo.xml", run_dir / "summary.xml")
    out = {"completion": m.get("completion_rate"), "time_loss": m["avg_time_loss_s"],
           "waiting": m["avg_waiting_s"], "peak_halting": m["peak_halting"],
           "p90_dur": m.get("duration_p90_s")}
    out.update(network_spread(run_dir))
    out.update(system_totals(run_dir))
    assignment = run_dir / "assignment.json"
    if assignment.exists():
        a = json.loads(assignment.read_text())
        out["entropy"], out["split"] = a.get("spread_entropy"), a.get("share")
    return out


def _job(args) -> str:
    scenario, seed, vehicles, horizon, obstacles, out_root, clearance = args
    from pipeline import run_scenarios
    run_scenarios.OUT = Path(out_root)
    with contextlib.redirect_stdout(io.StringIO()):
        run_scenarios.run(run_scenarios.SCENARIOS[scenario], vehicles, horizon, seed,
                          fcd_period=1.0, obstacles=obstacles, clearance=clearance)
    return str(Path(out_root) / scenario)


def _trips(run_dir: Path) -> dict[str, float]:
    return {e.get("id"): float(e.get("duration"))
            for e in ET.parse(run_dir / "tripinfo.xml").getroot().iter("tripinfo")}


LABEL = {"ue_corridor": "UE (selfish)", "hive_corridor": "Traffic Hive", "marl_corridor": "Hive (MARL)"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vehicles", type=int, default=1000)
    parser.add_argument("--horizon", type=int, default=900)
    parser.add_argument("--seeds", type=int, nargs="+", default=[11, 22, 33, 44, 55])
    parser.add_argument("--obstacles", type=str, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--clearance", type=int, default=1800,
                        help="seconds the run continues after the demand window so trips can finish")
    parser.add_argument("--rules", nargs="+", default=list(PAIR),
                        help="scenarios to compare; the first is the baseline "
                             "(e.g. ue_corridor hive_corridor marl_corridor)")
    args = parser.parse_args()
    rules, base_rule = args.rules, args.rules[0]

    tag = f"v{args.vehicles}" + ("_obs" if args.obstacles else "") + (f"_c{args.clearance}" if args.clearance != 600 else "")
    jobs = [(s, seed, args.vehicles, args.horizon, args.obstacles, str(ROOT / tag / f"s{seed}"), args.clearance)
            for seed in args.seeds for s in rules]
    with ProcessPoolExecutor(args.workers) as ex:
        list(ex.map(_job, jobs))

    rows = []
    for seed in args.seeds:
        base = ROOT / tag / f"s{seed}"
        summ = {r: summarise(base / r) for r in rules}
        trips = {r: _trips(base / r) for r in rules}
        tb = trips[base_rule]
        paired = {}
        for r in rules[1:]:
            both = [k for k in trips[r] if k in tb]
            paired[r] = round(sum(trips[r][k] < tb[k] for k in both) / max(1, len(both)), 4)
        rows.append({"seed": seed, "rules": summ, "paired_faster": paired})

    def agg(key: str, who: str) -> str:
        v = [r["rules"][who][key] for r in rows if r["rules"][who].get(key) is not None]
        return f"{st.mean(v):.3f} ± {st.stdev(v):.3f}" if len(v) > 1 else f"{v[0]:.3f}"

    print(f"\n{args.vehicles} vehicles, {'with' if args.obstacles else 'no'} obstacles, "
          f"{len(rows)} seeds (mean ± sd)")
    print(f"{'':<22}" + "".join(f"{LABEL.get(r, r):>22}" for r in rules))
    for k in ["vehicle_hours", "unfinished", "completion", "time_loss", "waiting", "peak_halting", "p90_dur",
              "edges_used", "gini_edge_load", "top10pct_edge_share", "entropy"]:
        print(f"{k:<22}" + "".join(f"{agg(k, r):>22}" for r in rules))
    win_rate = {}
    for r in rules[1:]:
        red = [(x["rules"][base_rule]["time_loss"] - x["rules"][r]["time_loss"]) / x["rules"][base_rule]["time_loss"] * 100 for x in rows]
        vred = [(x["rules"][base_rule]["vehicle_hours"] - x["rules"][r]["vehicle_hours"]) / x["rules"][base_rule]["vehicle_hours"] * 100 for x in rows]
        wins = sum(x["rules"][r]["vehicle_hours"] < x["rules"][base_rule]["vehicle_hours"] for x in rows)
        win_rate[r] = wins / len(rows)
        print(f"\n{LABEL.get(r, r)} vs {LABEL.get(base_rule, base_rule)}")
        print(f"  total vehicle-hours reduction per seed (%): {[round(x, 1) for x in vred]}")
        print(f"  time-loss reduction per seed (%, completed trips only): {[round(x, 1) for x in red]}")
        print(f"  win rate (lower total vehicle-hours): {wins}/{len(rows)}")
        print(f"  vehicles faster (paired): {[x['paired_faster'][r] for x in rows]}")
    if "hive_corridor" in rules and "marl_corridor" in rules:
        red = [(x["rules"]["hive_corridor"]["vehicle_hours"] - x["rules"]["marl_corridor"]["vehicle_hours"]) / x["rules"]["hive_corridor"]["vehicle_hours"] * 100 for x in rows]
        print(f"\nMARL vs Traffic Hive rule: total vehicle-hours change per seed (%, positive = MARL better): {[round(x, 1) for x in red]}")

    out = ROOT / f"eval_{tag}{'_' + '-'.join(rules) if rules != list(PAIR) else ''}.json"
    out.write_text(json.dumps({"args": vars(args), "rows": rows, "win_rate": win_rate}, indent=1))
    print(f"\nwritten {out}")


if __name__ == "__main__":
    main()
