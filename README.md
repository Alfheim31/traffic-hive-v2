# Traffic Hive — simulation and defense demo

Headless SUMO pipeline plus a React Native playback app, built so a panel can
watch 1000 vehicles move across the España corridor without SUMO's renderer in
the room.

The app does not simulate. SUMO computes trajectories ahead of time, the packer
quantises them into a flat binary, and the app plays that binary back. This
keeps SUMO as the validated engine (as Chapter 3 commits to), removes all
runtime lag, and guarantees the numbers on screen match the numbers in the
paper.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`eclipse-sumo` ships the binaries, so no separate SUMO install is needed.
Verify with `netconvert --version`.

## Pipeline

| Stage | Command | Produces |
|---|---|---|
| 1. Build network | `make net` | `data/net/corridor.net.xml` |
| 2. Audit features | `make audit` | signal / crossing / sidewalk counts |
| 3. Run scenarios | `make runs` | `data/out/<scenario>/{fcd,tripinfo,summary}.xml` |
| 4. Pack assets | `make pack` | `app/assets/sim/{net.json,traj_*.bin,metrics.json}` |
| 5. Calibrate | `make calibrate` | `data/out/calibration.json` |
| 6. Figures | `make figures` | `figures/fig*.png` and `.pdf` |

Or everything at once:

```bash
make all VEHICLES=1000 HORIZON=900 SEED=42
```

### Stage 2 is a gate, not a formality

Run `make audit` immediately after building the network. If it reports zero
traffic lights or zero crossings, the OSM extract lacks the tags and no amount
of frontend work will surface them. Fix the extract before continuing.

## Scenarios

| Name | Signals | Routing | Role |
|---|---|---|---|
| `fixed` | static | shortest path | naive baseline |
| `actuated` | gap-based | shortest path | strong baseline |
| `ue` | gap-based | greedy rerouting | what navigation apps do |
| `hive` | gap-based | HPDM softmax | the proposed rule |

All four share one network, one demand file, one seed. Only the rule changes.
That isolation is what makes the comparison defensible.

### On eta

The HPDM softmax sharpness constant is 0.03. Raising it collapses the softmax
onto the argmin, at which point `hive` becomes indistinguishable from `ue` and
the ablation measures nothing. Do not tune it upward to make results look
cleaner.

## Binary format

`traj_<scenario>.bin`, little-endian:

```
magic       4B    "THV1"
n_frames    u32
n_vehicles  u32
scale       f32   metres per quantisation unit
frame_dt    f32   seconds between frames
types       u8  * n_vehicles        0=car 1=jeepney 2=motorcycle
frames      n_frames * n_vehicles * (i16 x, i16 y, u8 speed_pct, u8 flags)
```

Absent vehicles are encoded `x = y = -32768`. `flags` bit 0 marks a stopped
vehicle so the renderer can colour queues without recomputing anything.

Roughly 6 bytes per vehicle per frame: 1000 vehicles at 2 Hz over 15 minutes is
about 11 MB per scenario.

## Known result worth keeping

On a small uncongested grid, HPDM loses to plain actuated control — mean time
loss rose 5.6% and mean route length rose from 997 m to 1019 m. This is
expected: with no congestion to redistribute, softmax assignment only adds
detour distance. HPDM wins only once mainline queueing cost exceeds detour
cost. Run a demand sweep to locate that crossover and report it; it is a
stronger result than a single comparison, and it pre-empts the obvious
question.

## Status

- [x] Stage 1 network builder with TLS / crossing / sidewalk / layer preservation
- [x] Stage 2 feature audit
- [x] Stage 3 demand generation and headless runner (fixed / actuated / ue / hive)
- [x] Stage 4 packer: net geometry, quantised trajectories, precomputed metrics
- [x] Stage 5 run-on-demand server with disk cache (`server/app.py`)
- [ ] Stage 6 React Native playback app (Skia canvas, transport controls)
- [ ] Stage 7 results panel wired to `metrics.json`
- [ ] Stage 8 demand sweep for the HPDM crossover plot


## Server

The app collects a vehicle count and a duration and posts them here. Runs are
cached on disk by parameter hash, so a repeated combination returns instantly.

```bash
pip install fastapi uvicorn
python -m server.app --prewarm 250 500 1000 2000 --durations 60 --serve
```

Pre-warm the combinations you intend to demonstrate so the demo itself is all
cache hits, while leaving the live path available for anything asked on the
spot. The server prints its LAN address at startup; point the app at that.

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness, available scenarios, cache size |
| `POST /run` | start or retrieve a run; returns a key |
| `GET /status/{key}` | progress for the app's progress bar |
| `GET /assets/{key}/{file}` | `net.json`, `metrics.json`, `traj_*.bin` |
| `GET /runs` | cached runs, for a recent-runs picker |


## Analysis modules

| Module | Library | Purpose |
|---|---|---|
| `pipeline/theory.py` | NumPy | Exhaustive enumeration of UE, SO, SSO and the constrained optimum; Price of Anarchy |
| `pipeline/calibrate.py` | SciPy | Fits BPR volume-delay parameters to SUMO observations by nonlinear least squares |
| `pipeline/figures.py` | Matplotlib | Regenerates every manuscript figure from data files |

### Technology stack

| Tool | Role |
|---|---|
| SUMO | Microsimulation engine; the validation reference |
| TraCI / sumolib | Runtime control and network parsing |
| NumPy | Allocation enumeration, aggregation, array packing |
| SciPy | Volume-delay calibration (`optimize.curve_fit`) |
| Matplotlib | Publication figures |
| FastAPI / Uvicorn | Run-on-demand simulation server |
| React Native (Expo) + Skia | Playback and results interface |

Unity is not used. Three-dimensional rendering was considered during design
and dropped: the Skia canvas covers the visualisation requirement on both web
and mobile from one codebase, and a separate engine would have added a build
dependency without changing any result.

### Calibration

`beta = 0.15` and `n = 4` are United States Bureau of Public Roads defaults.
Fitting them against this network gave `n = 1.80`, `beta = 0.384`, with
R-squared 0.9974 against 0.9082 for the defaults — the network congests more
gradually but from a lower threshold than the standard assumes. Re-run
`make calibrate` after any change to the network and report the fitted
values rather than the defaults.

### On the analytical corridor parameters

`pipeline/theory.py` ships with placeholder free-flow times. Override them
with `--t0`, or fit them via `make calibrate`, before quoting any figure it
produces. The capacities match the manuscript; the free-flow times do not.

## Roadside obstacles

Metro Manila corridors lose capacity to things OSM does not contain: vendors
in the curb lane, jeepneys loading mid-block, enforcers holding a junction,
stalled vehicles and road work. `pipeline/obstacles.py` applies these through
TraCI during the run, so they shape the trajectories instead of being drawn
on top of them. The Hive controller is never told where they are; it sees
the slower edges through travel times and routes around them.

| Type | Effect in SUMO (light / moderate / heavy) |
|---|---|
| `vendor` | curb lane at 60 / 40 / 25 % of its limit; heavy closes it on multi-lane roads |
| `jeepney_stop` | passing jeepneys stop on the curb lane for 15 / 30 / 60 s |
| `enforcer` | green phases at the junction held 1.5 / 2 / 2.5× longer |
| `stalled` | curb lane closed |
| `roadwork` | curb lane closed, next lane at 50 % |

Coordinates are the local metre grid of `net.json` (origin at the network
centre, y up). The easiest way to make a file is to place obstacles in the
Hive Out page and press **Copy obstacles JSON**.

```bash
python -m pipeline.run_scenarios --scenario hive --vehicles 1000 --horizon 900 \
    --fcd-period 1.0 --obstacles examples/obstacles_espana_demo.json
python -m pipeline.pack --scenarios hive --baseline hive
```

`pack` now also writes `signals_<scenario>.bin` (distance to the next stop
line and the real signal state for every vehicle and frame),
`vehicles_<scenario>.json` (trip metadata) and `obstacles_<scenario>.json`.

## Hive Out driver view

`hive_out/index.html` is the split-screen demo: the simulation on the left,
an obstacle editor, and the Hive Out phone with a 3D chase view of any
vehicle you click. Served by the simulation server it can start runs:

```bash
python -m server.app --serve        # then open http://<host>:8000/hive-out/
```

Enter the number of vehicles (and the demand window) at the top, place
obstacles on the map (or add several along one street), and press
**Simulate**. The server runs `ue_corridor` and `hive_corridor` on identical
demand with those obstacles and the page loads both: switch the playback
between **User equilibrium** and **Traffic Hive** (the followed vehicle is
kept, so the same driver's trip can be compared), and open **Results &
comparison** for the same measures as the app (delay, waiting, peak queue,
trip time, queue over time, synchronisation and fairness) plus how evenly
each rule spread traffic over the roads. Runs already cached on the server
appear as quick-pick vehicle counts. Obstacles added after a run are marked **Draft**
until they have been simulated.

## Corridor routing on measured costs

The corridor controller used to price corridors with a BPR curve and a
pipe-model capacity. On signalised urban corridors that model barely rises
until a corridor is nearly full, because the real delay comes from queues
at junctions. Every vehicle kept seeing the free-flow-fastest corridor as
cheapest, so demand piled onto it (an 83 / 15 / 2 % split on España).

It now measures each corridor's travel time every epoch from SUMO's
per-edge estimates and derives marginal cost from it. With
T − T0 = T0·β·(N/C)ⁿ,

    MC = T0 [1 + (n+1) β (N/C)ⁿ] = T0 + (n+1)(T_obs − T0)

so no capacity estimate is needed. Measurements are smoothed (weight 0.4 on
the newest) to damp feedback oscillation, each assignment adds its expected
delay n(T − T0)/N until the next measurement so a burst of departures does
not herd, and the fairness tolerance ε is applied to expected travel time
(what a driver experiences), not to marginal cost. The UE baseline uses the
same measured travel times, so the comparison is like for like.

`python -m pipeline.evaluate` runs both rules on identical demand over
several seeds. Results on España (rebuilt network, 1000 vehicles, 15-minute
demand window, 5 seeds):

| | UE (selfish) | Traffic Hive |
|---|---|---|
| Trips completed | 96.5 % | 96.1 % |
| Time loss per trip | 244 s | 216 s (−11 %) |
| Waiting per trip | 115 s | 97 s (−15 %) |
| Peak queue | 236 | 202 (−15 %) |
| Gini of road-segment load | 0.783 | 0.734 |
| Busiest 10 % of segments carry | 67.5 % | 61.3 % |
| Corridor split entropy | 0.74 | 0.90 |
| With the 12 España obstacles: time loss | 253 s | 237 s (−6 %, Hive ahead in 5/5 seeds) |

Hive reduced time loss in 9 of the 10 seeded runs. About half of individual
vehicles are faster under Hive than under UE; a system-optimal rule cannot
make every driver faster, since some take a slightly longer corridor so that
the rest save more, and ε bounds that sacrifice.
