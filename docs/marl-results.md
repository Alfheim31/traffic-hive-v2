# Learning-based corridor assignment: results

*Draft for Chapter 4. All numbers come from `python -m pipeline.evaluate` on the OSM-built España network (`data/net/corridor.net.xml`), with the Hive rule using the calibration in `data/out/calibration.json` (β = 0.304, n = 1.0).*

## Evaluation protocol

Each method was run on identical demand for ten held-out seeds (11, 22, …, 110): 1,000 vehicles over a 900 s demand window, followed by 1,800 s of clearance so that nearly every trip completes. Each condition was run without obstacles and with the twelve España obstacles (`examples/obstacles_espana_demo.json`).

The primary measure is **total vehicle-hours**: the time of every vehicle in the network or waiting to enter it, summed over the run, whether or not it finished. It is free of survivorship bias, and it is the quantity a system-optimal assignment minimises. Per-trip time loss and waiting are secondary measures, because they are averaged over completed trips only.

Methods compared:

| Label | Scenario | What assigns the corridors |
|---|---|---|
| UE | `ue_corridor` | Every vehicle takes the currently fastest corridor (selfish routing) |
| Hive rule | `hive_corridor` | HPDM: softmax over marginal cost, η = 0.03, ε = 0.25 |
| Learned split, 200 it. | `marl_corridor` | MAPPO policy choosing the split, trained 200 iterations on 5-minute episodes |
| Learned split, 260 it. | `marl_corridor` | The same policy fine-tuned for 60 more iterations on 15-minute episodes |
| Hive + MARL | `hive_marl_corridor` | The Hive rule, with MAPPO agents adding price offsets each epoch (residual mode), trained 150 iterations on 15-minute episodes |

## Training

**Learned split.** Trained for 200 MAPPO iterations (800 SUMO episodes) on 5-minute demand windows of 280–420 vehicles, half of them with obstacles. The mean episode return rose from −78 to about −46 by iteration 75 and then levelled off (Figure 7). The corridor split moved from 33 / 33 / 33 % to about 65 / 25 / 10 %.

**Episode-length sensitivity.** Under the conditions it was trained on (350 vehicles over 300 s, four seeds), the 200-iteration policy matched the Hive rule: 34.2 against 34.6 vehicle-hours, lower in 3 of 4 seeds, with a split of 71 / 21 / 8 %. Under the evaluation conditions (1,000 vehicles over 900 s) it changed behaviour, sending about 41 / 43 / 16 % of vehicles down the three corridors, and lost to both baselines in every seed. Inputs that depend on the scale of the run (time as a fraction of the demand window, vehicles in the network) reach values never seen in training. Fine-tuning for 60 iterations on 15-minute episodes (900–1,100 vehicles) improved the training return from about −192 to −170 but kept the split near 42 / 40 / 18 %.

**Hive + MARL.** Trained for 150 iterations directly on 15-minute episodes. The return moved sideways (about −158 over the first ten iterations and −157 over the last ten; Figure 8), and the policy's entropy fell only from 2.2 to 1.9, against 2.56 for a uniform choice among the 13 actions. The agents did not find an adjustment that the reward consistently favoured.

## Results

**Table A. No obstacles** (mean ± standard deviation over ten seeds)

| Measure | UE | Hive rule | Learned split, 200 it. | Learned split, 260 it. | Hive + MARL |
|---|---|---|---|---|---|
| Total vehicle-hours | 114.4 ± 3.3 | **112.9 ± 2.8** | 124.8 ± 2.0 | 119.1 ± 3.7 | 113.6 ± 3.4 |
| Trips completed | 100 % | 100 % | 100 % | 100 % | 100 % |
| Time loss per trip (s) | 173.9 | **160.7** | 188.8 | 171.1 | 163.5 |
| Waiting per trip (s) | 76.4 | **69.3** | — | 78.1 | 70.9 |
| Peak queue (vehicles) | 187.7 | 169.8 | — | **151.6** | 172.8 |
| Load on busiest 10 % of segments | 57.2 % | 53.3 % | — | **52.4 %** | 53.5 % |
| Road-load Gini | 0.717 | 0.684 | — | **0.666** | 0.684 |
| Corridor split entropy | 0.76 | 0.86 | — | **0.92** | 0.86 |

**Table B. With the twelve España obstacles**

| Measure | UE | Hive rule | Learned split, 200 it. | Learned split, 260 it. | Hive + MARL |
|---|---|---|---|---|---|
| Total vehicle-hours | 163.6 ± 4.9 | 163.4 ± 3.6 | 196.1 ± 3.7 | 179.6 ± 2.5 | **161.6 ± 6.5** |
| Vehicles unfinished at end | 63.7 | 57.4 | 85.5 | 71.8 | **53.6** |
| Trips completed | 93.6 % | 94.3 % | 91.5 % | 92.8 % | **94.6 %** |
| Time loss per completed trip (s) | 228.2 | **220.1** | 283.2 | 249.8 | 220.8 |
| Waiting per completed trip (s) | 123.5 | 116.7 | — | 135.5 | **115.2** |
| Peak queue (vehicles) | 250.0 | 242.4 | — | **213.8** | 243.9 |
| Load on busiest 10 % of segments | 66.4 % | 63.0 % | — | 65.2 % | **62.9 %** |
| Road-load Gini | 0.770 | **0.739** | — | 0.751 | **0.739** |
| Corridor split entropy | 0.83 | 0.93 | — | **0.95** | 0.92 |

Dashes: measure not recorded for that run.

**Per seed, total vehicle-hours** (seeds 11, 22, …, 110)

| | No obstacles | With obstacles |
|---|---|---|
| UE | 115.2, 117.4, 113.9, 109.0, 113.9, 114.8, 118.6, 116.6, 116.0, 108.4 | 175.5, 164.3, 162.0, 161.8, 165.5, 166.4, 160.6, 162.1, 157.8, 160.2 |
| Hive rule | 115.4, 112.3, 114.3, 110.5, 115.6, 113.5, 110.1, 114.6, 115.7, 107.2 | 163.9, 168.9, 159.4, 166.2, 165.0, 165.4, 163.7, 163.3, 161.6, 156.1 |
| Learned split, 200 it. | 125.3, 125.1, 122.5, 124.5, 127.0, 128.6, 125.2, 123.1, 122.0, 124.4 | 196.8, 196.1, 190.1, 199.9, 199.8, 198.4, 192.1, 200.4, 191.7, 195.9 |
| Learned split, 260 it. | 122.8, 123.0, 116.7, 112.6, 123.0, 121.1, 118.6, 115.8, 121.0, 116.1 | 184.6, 180.5, 175.8, 178.8, 180.6, 181.3, 176.9, 178.9, 180.2, 178.0 |
| Hive + MARL | 113.6, 115.1, 113.4, 114.7, 112.9, 114.9, 111.5, 120.2, 113.3, 106.4 | 167.0, 159.3, 157.7, 174.8, 167.6, 154.7, 156.3, 162.8, 160.2, 155.4 |

**Paired comparisons on total vehicle-hours**

| Comparison | No obstacles | With obstacles |
|---|---|---|
| Hive rule vs UE | lower in 6 of 10 seeds, −1.3 % | lower in 5 of 10, −0.2 % |
| Learned split (200 it.) vs Hive rule | higher in 10 of 10, +10.5 % | higher in 10 of 10, +20.0 % |
| Learned split (260 it.) vs Hive rule | higher in 10 of 10, +5.5 % | higher in 10 of 10, +9.9 % |
| Hive + MARL vs Hive rule | lower in 5 of 10, +0.6 % (paired t = −0.8) | lower in 7 of 10, −1.1 % (paired t = 0.9) |

With ten seeds, a paired t of about 2.3 or more in magnitude would be needed for significance at the 5 % level, so neither Hive + MARL difference is significant.

## Discussion

**The Hive rule.** Against selfish routing, the rule cuts time loss per completed trip in all ten seeds without obstacles (−7.6 %) and in eight of ten with them (−3.5 %). It also lowers peak queues and spreads load more evenly. With obstacles it leaves 10 % fewer vehicles unfinished. On total vehicle-hours the difference is small: about 1 % lower without obstacles and level with them. Per-trip improvements should therefore be reported together with completion, and network-wide claims should rest on vehicle-hours.

**Learning the split from scratch.** The MAPPO policy learned, from reward alone, a split close to the rule's under the conditions it was trained on, and matched the rule there. It did not transfer to longer runs. Because evaluation uses 15-minute demand windows, it lost to both baselines in every seed. Fine-tuning on 15-minute episodes halved the gap but did not close it within 60 iterations. The fine-tuned policy does reduce peak queues (about 11–12 % below the rule), but by holding vehicles back rather than moving them faster, which shows up as more waiting and more unfinished vehicles.

**Learning adjustments to the rule.** Keeping the rule in charge and letting the agents adjust its prices produced a method that is no worse than the rule on average, which was the purpose of the design, although single seeds still vary by up to about 5 % either way. It did not produce a significant improvement. With obstacles it was ahead in 7 of 10 seeds, with 1.1 % fewer vehicle-hours and 6.6 % fewer unfinished vehicles. That is consistent with a small gain, but it is within seed-to-seed variation. The flat learning curve suggests the agents mostly leave the rule unchanged.

**Why the margins are small.** Every method we tested lies within about 2 % of the others on total vehicle-hours, while results vary by about 3 % from seed to seed. This matches the narrow price-of-anarchy window found for this network: when selfish routing is already close to the system optimum, neither a hand-designed rule nor a learned policy has much to recover. We therefore treat the Hive rule as the main result and report the MARL extensions as a tested alternative that did not outperform it.

## Caveats

- **Seeds.** Ten seeds can detect a difference of roughly 2–3 % in vehicle-hours. Testing whether the 1.1 % Hive + MARL gain with obstacles is real needs more, for example 30 seeds (`--seeds` with 30 values; about three hours on a laptop).
- **Training conditions.** The learned split policy is sensitive to the length and size of training episodes. Training it from the start on 15-minute episodes, or making its inputs independent of run length, might change the result. We did not test that.
- **Calibration.** The calibration fit sits at the exponent's lower bound (n = 1.0), so the marginal-cost term may be slightly misestimated. The first 46 Hive + MARL training iterations used the earlier fallback values (β = 0.384, n = 1.80) before training continued with the fitted ones. Evaluation used the fitted values throughout.
