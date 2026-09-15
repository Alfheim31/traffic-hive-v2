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
