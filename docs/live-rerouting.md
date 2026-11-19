# Live rerouting and the real-time digital twin

*Draft section for Chapter 3 (method) and Chapter 4 (results). Numbers are from SUMO 1.28 on the OSM-built España network with `data/out/calibration.json`. Re-run them on the machine that produces the manuscript's other figures, using the commands at the end, before quoting them.*

## Method

The Traffic Hive rule (`hive_corridor`) assigns each vehicle to a corridor once, when it departs. A vehicle already on the road keeps that corridor even if an incident later blocks it. With the twelve España obstacles, about 6 % of vehicles never finish under either the rule or selfish routing.

`hive_reroute_corridor` keeps the departure rule unchanged and adds en-route reassignment (`HiveRerouteController` in `pipeline/run_scenarios.py`). Every 30 s it reviews the vehicles on the road:

1. **Is the vehicle affected?** It is a candidate when the measured travel time of the rest of its route is at least 1.5 times the free-flow time *and* at least 60 s longer. Measured times are SUMO's current per-edge travel times, capped at 300 s per edge. That is what an incident or a spill-back ahead of the vehicle looks like.
2. **Is there a clearly better route?** The candidate is rerouted on current travel times. The new route is kept only if it saves at least 15 % of the remaining time and at least 30 s. Otherwise the old route is restored.
3. **Anti-oscillation rules.**
   - A vehicle switches at most twice, and not again within 120 s.
   - Each accepted switch adds 2 s to every edge of the new route for the rest of that review, so later vehicles see the alternative as busier and do not all pile onto it.

Those rules already stop the route flapping that naive dynamic rerouting causes. We also tested reconsidering only a random fraction of candidates per review: 30 %, 60 % and 100 % gave 137.3, 133.4 and 132.4 total vehicle-hours with obstacles (10 seeds). Thinning only delayed the response to incidents, so all candidates are reconsidered.

**Baseline.** `ue_reroute_corridor` applies the same en-route rerouting to selfish departure choice. That is how navigation apps behave: every vehicle takes the fastest corridor, and a delayed vehicle is sent to its currently fastest route. The two rerouting scenarios differ only in how departing vehicles are assigned. Comparing them separates the effect of rerouting from the effect of the Hive rule.

## Results

Ten seeds (11, 22, …, 110), 1,000 vehicles over 900 s, 1,800 s clearance. All four methods ran on identical demand. Means are over the ten seeds.

**With the twelve España obstacles**

| Measure | UE | Hive rule | UE + live rerouting | Hive + live rerouting |
|---|---|---|---|---|
| Total vehicle-hours | 162.5 | 162.0 | 132.8 | **132.4** |
| Vehicles unfinished | 62.4 | 57.1 | 9.7 | **6.4** |
| Trips completed | 93.8 % | 94.3 % | 99.0 % | **99.4 %** |
| Time loss per completed trip (s) | 226.9 | 216.4 | 214.3 | **208.4** |
| Waiting per completed trip (s) | 122.2 | 113.8 | 110.3 | **106.8** |
| Peak queue (vehicles) | 237.3 | 247.0 | 238.7 | **227.9** |
| Load on busiest 10 % of segments | 66.3 % | 63.1 % | 62.2 % | **58.8 %** |
| Road-load Gini | 0.769 | 0.740 | 0.744 | **0.716** |

**No obstacles**

| Measure | UE | Hive rule | UE + live rerouting | Hive + live rerouting |
|---|---|---|---|---|
| Total vehicle-hours | 113.1 | 114.1 | 111.4 | **110.7** |
| Time loss per trip (s) | 170.4 | 163.2 | 166.5 | **154.7** |
| Waiting per trip (s) | 74.0 | 71.0 | 73.6 | **64.9** |
| Peak queue (vehicles) | 184.3 | 172.8 | 190.9 | **171.2** |
| Load on busiest 10 % of segments | 56.9 % | **53.0 %** | 58.3 % | 54.0 % |
| Road-load Gini | 0.716 | **0.680** | 0.728 | 0.693 |

Every trip completes without obstacles under all four methods.

**Paired comparisons for Hive + live rerouting** (better in *k* of 10 seeds; mean change; paired t, where |t| ≥ 2.26 is significant at 5 % with 9 degrees of freedom)

| Against | Condition | Total vehicle-hours | Time loss per trip | Unfinished vehicles |
|---|---|---|---|---|
| UE (today's navigation, no rerouting) | obstacles | 10/10, −18.5 %, t = 14.3 | 9/10, −8.2 %, t = 3.5 | 10/10, −90 %, t = 23.3 |
| | none | 8/10, −2.2 %, t = 2.3 | 10/10, −9.2 %, t = 4.8 | — |
| UE + live rerouting (navigation-app rerouting) | obstacles | 4/10, −0.3 %, t = 0.3 | 7/10, −2.8 %, t = 1.6 | 7/10, −34 %, t = 2.1 |
| | none | 6/10, −0.7 %, t = 0.9 | 10/10, −7.1 %, t = 4.6 | — |
| Hive rule without rerouting | obstacles | 10/10, −18.3 %, t = 24.0 | 8/10, −3.7 %, t = 2.2 | 10/10, −89 %, t = 18.9 |
| | none | 9/10, −3.0 %, t = 3.6 | 9/10, −5.2 %, t = 4.3 | — |

## What the results show

- **Live rerouting is what clears incidents.** It cuts total vehicle-hours by about 18 % with obstacles and nearly eliminates unfinished trips, whichever departure rule it is combined with.
- **The Hive rule adds per-trip gains.** On top of rerouting, Hive's departure assignment gives:
  - significantly lower time loss per trip without obstacles: 7 % below navigation-app rerouting, in 10 of 10 seeds;
  - about a third fewer stranded vehicles with obstacles (borderline significant);
  - the most even road use with obstacles.

  It does not significantly reduce total vehicle-hours compared with navigation-app rerouting.
- **Against today's baseline**, selfish routing without rerouting, Hive + live rerouting is better on every measure in both conditions.

## Real-time operation

`server/live.py` runs the simulation step by step while the page `hive_out/live.html` is open. Two simulations run side by side on identical demand: selfish routing and Hive + live rerouting. The page sends obstacles to both at the same simulated moment. Each frame reports how long the controller's decision work took.

On a 2-core cloud machine with 1,000 vehicles and the twelve obstacles, measured over a full live run:

- Hive + live rerouting spent on average 28 ms of decision work per half-second simulation step, and at most 39 ms averaged over any one minute.
- The 95th percentile of single steps was below 275 ms. Those are the steps where all 48 corridors are re-measured (every 10 s) or vehicles are reviewed (every 30 s).
- Selfish routing, which also re-measures the corridors, averaged 25 ms.

Real time allows 500 ms per step, so the controller keeps up with real time with room to spare. On this machine the live page ran up to about 8× faster than real time with both simulations running.

## Reproduce

```bash
python -m pipeline.evaluate --vehicles 1000 \
    --rules ue_corridor hive_corridor ue_reroute_corridor hive_reroute_corridor \
    --seeds 11 22 33 44 55 66 77 88 99 110
python -m pipeline.evaluate --vehicles 1000 \
    --rules ue_corridor hive_corridor ue_reroute_corridor hive_reroute_corridor \
    --seeds 11 22 33 44 55 66 77 88 99 110 --obstacles examples/obstacles_espana_demo.json
python -m server.app --serve     # then open http://localhost:8000/hive-out/live.html
```
