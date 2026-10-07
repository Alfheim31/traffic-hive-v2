"""Replicated evaluation of the Hive corridor rule against selfish routing.

Runs `hive_corridor` and `ue_corridor` on identical demand for several
seeds, optionally with roadside obstacles, and reports:

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
  win_rate              share of seeds where Hive's mean time loss is
                        below UE's
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


def summarise(run_dir: Path) -> dict:
    m = pack.compute_metrics(run_dir / "tripinfo.xml", run_dir / "summary.xml")
    out = {"completion": m.get("completion_rate"), "time_loss": m["avg_time_loss_s"],
           "waiting": m["avg_waiting_s"], "peak_halting": m["peak_halting"],
           "p90_dur": m.get("duration_p90_s")}
    out.update(network_spread(run_dir))
    assignment = run_dir / "assignment.json"
    if assignment.exists():
        a = json.loads(assignment.read_text())
        out["entropy"], out["split"] = a.get("spread_entropy"), a.get("share")
    return out


def _job(args) -> str:
    scenario, seed, vehicles, horizon, obstacles, out_root = args
    from pipeline import run_scenarios
    run_scenarios.OUT = Path(out_root)
    with contextlib.redirect_stdout(io.StringIO()):
        run_scenarios.run(run_scenarios.SCENARIOS[scenario], vehicles, horizon, seed,
                          fcd_period=1.0, obstacles=obstacles)
    return str(Path(out_root) / scenario)


def _trips(run_dir: Path) -> dict[str, float]:
    return {e.get("id"): float(e.get("duration"))
            for e in ET.parse(run_dir / "tripinfo.xml").getroot().iter("tripinfo")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vehicles", type=int, default=1000)
    parser.add_argument("--horizon", type=int, default=900)
    parser.add_argument("--seeds", type=int, nargs="+", default=[11, 22, 33, 44, 55])
    parser.add_argument("--obstacles", type=str, default=None)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    tag = f"v{args.vehicles}" + ("_obs" if args.obstacles else "")
    jobs = [(s, seed, args.vehicles, args.horizon, args.obstacles, str(ROOT / tag / f"s{seed}"))
            for seed in args.seeds for s in PAIR]
    with ProcessPoolExecutor(args.workers) as ex:
        list(ex.map(_job, jobs))

    rows = []
    for seed in args.seeds:
        base = ROOT / tag / f"s{seed}"
        ue, hive = summarise(base / PAIR[0]), summarise(base / PAIR[1])
        tu, th = _trips(base / PAIR[0]), _trips(base / PAIR[1])
        both = [k for k in th if k in tu]
        rows.append({"seed": seed, "ue": ue, "hive": hive,
                     "paired_faster": round(sum(th[k] < tu[k] for k in both) / max(1, len(both)), 4)})

    def agg(key: str, who: str) -> str:
        v = [r[who][key] for r in rows if r[who].get(key) is not None]
        return f"{st.mean(v):.3f} ± {st.stdev(v):.3f}" if len(v) > 1 else f"{v[0]:.3f}"

    wins = sum(r["hive"]["time_loss"] < r["ue"]["time_loss"] for r in rows)
    print(f"\n{args.vehicles} vehicles, {'with' if args.obstacles else 'no'} obstacles, "
          f"{len(rows)} seeds (mean ± sd)")
    print(f"{'':<22}{'UE (selfish)':>22}{'Traffic Hive':>22}")
    for k in ["completion", "time_loss", "waiting", "peak_halting", "p90_dur",
              "edges_used", "gini_edge_load", "top10pct_edge_share", "entropy"]:
        print(f"{k:<22}{agg(k, 'ue'):>22}{agg(k, 'hive'):>22}")
    red = [(r["ue"]["time_loss"] - r["hive"]["time_loss"]) / r["ue"]["time_loss"] * 100 for r in rows]
    print(f"\ntime-loss reduction per seed (%): {[round(x, 1) for x in red]}")
    print(f"win rate (Hive time loss below UE): {wins}/{len(rows)}")
    print(f"vehicles faster under Hive (paired): {[r['paired_faster'] for r in rows]}")

    out = ROOT / f"eval_{tag}.json"
    out.write_text(json.dumps({"args": vars(args), "rows": rows, "win_rate": wins / len(rows)}, indent=1))
    print(f"\nwritten {out}")


if __name__ == "__main__":
    main()
