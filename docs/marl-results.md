# Learning-based corridor assignment: results

*Draft for Chapter 4. All numbers come from `python -m pipeline.evaluate` on the rebuilt España network (see the caveat at the end). Re-run on the OSM-built `corridor.net.xml` before quoting them in the manuscript.*

## Training

The shared policy was trained for 188 MAPPO iterations (752 SUMO episodes). Figure 7 (`figures/fig7_marl_learning_curve.png`) shows the mean episode return per iteration.

In phase 1 the return rose from −80.8, which is the untrained policy choosing splits almost at random, to the level of the heuristic Traffic Hive rule (−55.0 on four fixed held-out episodes) by about iteration 65, and stayed there. Over the same period the policy's entropy fell from 2.71 (uniform over 15 splits) to about 1.1, and the corridor split moved from 33 / 33 / 33 % to about 56 / 28 / 16 %, which is close to the 65 / 26 / 9 % the heuristic rule produces. The agents therefore learned, from reward alone, to favour each OD pair's fastest corridor while keeping a meaningful share on the alternatives.

On the four held-out phase-1 episodes the greedy policy scored −55.6, against −55.0 for the heuristic rule and −56.5 for sending every vehicle down its fastest corridor.

Phase 1 cut episodes off 300 s after the demand window, while trips take about eight minutes, so about 40 % of vehicles were still travelling at the cut-off and their remaining cost was under-counted. Phase 2 resumed with a 600 s tail and a 600 s stranded cost, which reduced vehicles still travelling at the cut-off to about 6 %. Returns in phase 2 levelled off after about 10 iterations and training was stopped at iteration 188.

## Evaluation protocol

Each rule was run on identical demand for five held-out seeds (11, 22, 33, 44, 55), 1,000 vehicles over a 900 s demand window, with 1,800 s of clearance after the window so that nearly every trip completes. The primary measure is **total vehicle-hours**: the time of all vehicles in the network or waiting to enter it, summed over the run, whether or not they finished. It is free of survivorship bias, and it is the quantity a system-optimal assignment minimises. Per-trip time loss and waiting are reported as secondary measures because they are averaged over completed trips only.

An earlier evaluation with only 600 s of clearance showed the learned policy with the lowest per-trip time loss, but it also left almost twice as many vehicles unfinished. On total vehicle-hours it was no better than selfish routing. That result is not reported as an improvement; it is the reason the protocol above uses long clearance and total vehicle-hours.

## Results

**Table A. No obstacles** (mean ± standard deviation over five seeds)

| Measure | UE (selfish) | Traffic Hive rule | Traffic Hive MARL |
|---|---|---|---|
| Total vehicle-hours | 145.5 ± 7.0 | **144.5 ± 4.5** | 149.5 ± 10.4 |
| Vehicles unfinished at end | **16.0** | 26.0 | 23.6 |
| Trips completed | **98.4 %** | 97.4 % | 97.6 % |
| Time loss per completed trip (s) | 255.7 | **224.5** | 232.1 |
| Waiting per completed trip (s) | 124.2 | **105.1** | 115.7 |
| Peak queue (vehicles) | 236.4 | 202.0 | **199.4** |
| Load on busiest 10 % of road segments | 69.2 % | 64.3 % | **61.0 %** |
| Road-load Gini | 0.793 | 0.753 | **0.731** |
| Corridor split entropy | 0.74 | 0.90 | **0.96** |

Per seed, total vehicle-hours: UE 136.5 / 141.2 / 147.3 / 147.8 / 154.9; rule 144.2 / 137.7 / 143.5 / 148.8 / 148.5; MARL 156.6 / 147.2 / 162.8 / 136.4 / 144.7. MARL had lower vehicle-hours than the rule in 2 of 5 seeds and than UE in 2 of 5.

**Table B. With the twelve España obstacles**

| Measure | UE (selfish) | Traffic Hive rule | Traffic Hive MARL |
|---|---|---|---|
| Total vehicle-hours | 181.1 ± 5.6 | 185.2 ± 4.3 | **175.7 ± 5.3** |
| Vehicles unfinished at end | 67.6 | 69.6 | **55.4** |
| Trips completed | 93.2 % | 93.0 % | **94.5 %** |
| Time loss per completed trip (s) | 274.3 | 260.4 | **247.1** |
| Waiting per completed trip (s) | 142.5 | 135.8 | **126.9** |
| Peak queue (vehicles) | 262.2 | 242.2 | **229.0** |
| Load on busiest 10 % of road segments | 70.8 % | 69.1 % | **65.4 %** |
| Road-load Gini | 0.801 | 0.785 | **0.760** |
| Corridor split entropy | 0.84 | 0.93 | **0.98** |

Per seed, total vehicle-hours: UE 180.6 / 174.1 / 186.8 / 177.4 / 186.6; rule 188.1 / 185.6 / 182.4 / 179.5 / 190.2; MARL 177.9 / 181.2 / 178.1 / 167.4 / 173.9. MARL had lower vehicle-hours than the rule in **5 of 5 seeds** (by 2.4 %, 2.4 %, 5.4 %, 6.7 % and 8.6 %) and than UE in 4 of 5.

## Discussion

With obstacles, the learned policy is the best of the three rules on every measure: about 5 % fewer total vehicle-hours than the heuristic rule and 3 % fewer than selfish routing, more trips completed, shorter queues and the most even use of the road network. Because it also leaves fewer vehicles unfinished, the improvement is not an artefact of stranded vehicles. Half of the training episodes contained obstacles, so the policy has learned to respond to disruptions that the heuristic rule's fixed formula prices only through measured travel time, after the queue has formed.

Without obstacles, the learned policy spreads traffic the most evenly but does not reduce total vehicle-hours: it is about 3.5 % worse than the heuristic rule on average and varies more between seeds. On undisrupted demand the heuristic rule already sits close to what corridor reassignment can achieve, which is consistent with the narrow price-of-anarchy window reported for this network.

The evaluation also changes how the heuristic rule should be reported. On per-trip time loss it improves on selfish routing by about 12 % without obstacles and 5 % with them, but on total vehicle-hours the two are within 1 % without obstacles, and the rule is 2 % worse with them. In both cases the rule leaves more vehicles unfinished, and those vehicles cancel the time saved by the rest. The manuscript should state per-trip improvements together with completion, and use total vehicle-hours for any claim about network-wide congestion.

## Caveats

These runs use a SUMO network rebuilt from the exported `net.json`, whose junction connections are re-guessed by netconvert. Some vehicles that never finish may be stuck at reconstructed junctions rather than in genuine congestion, and the alternative corridors, which the Hive rules use more, pass through more of those junctions. The whole evaluation should be repeated on the OSM-built `corridor.net.xml`, and the policy retrained on it (`python -m pipeline.marl train`), before these figures are used in the manuscript.

Five seeds are enough to see a consistent 5-of-5 pattern with obstacles, but not to establish small differences without them. For the paper, run 10 seeds (`--seeds 11 22 33 44 55 66 77 88 99 110`) and report the paired per-seed differences.
