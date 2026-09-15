# Traffic Hive v2 — setup walkthrough

Every step from an empty folder to a running simulation, in order. Each step
ends with something you can check, so you never move forward on a broken
foundation.

Estimated time: 35–50 minutes, most of it waiting on installs.

---

## Step 0 — Prerequisites

Check what you already have:

```bash
python --version     # need 3.10 or newer
git --version
node --version       # need 18 or newer, for the app later
```

Missing anything:

- **Python** — python.org/downloads. On Windows, tick "Add Python to PATH".
- **Git** — git-scm.com/downloads
- **Node** — nodejs.org, LTS build

You do **not** need to install SUMO separately. It arrives as a Python package
in Step 4.

---

## Step 1 — Create the local project

```bash
mkdir traffic-hive-v2
cd traffic-hive-v2
git init
```

If git warns about the default branch name, set it now so it matches GitHub:

```bash
git branch -M main
```

**Check:** `ls -a` shows a `.git` folder.

---

## Step 2 — Get the project files

Download `traffic-hive-v2.zip` and extract it. It already contains the full
folder structure:

```
traffic-hive-v2/
├── README.md
├── SETUP.md
├── Makefile
├── requirements.txt
├── .gitignore
├── pipeline/
│   ├── __init__.py
│   ├── build_network.py
│   ├── run_scenarios.py
│   └── pack.py
├── server/
│   ├── __init__.py
│   └── app.py
├── data/
│   ├── raw/          <- your map.osm goes here in Step 6
│   ├── net/
│   └── out/
└── app/              <- the Expo app is created here in Step 11
```

If you extracted the zip somewhere other than where you ran `git init` in
Step 1, either move the extracted folder's contents into that directory, or
simply `cd` into the extracted `traffic-hive-v2` folder and run `git init`
there instead. Only one of the two folders should end up being the repo.

The empty `data/` subfolders contain a `.gitkeep` file each. That is
deliberate: git does not track empty directories, and the pipeline expects
these paths to exist.

**Check:**

```bash
ls pipeline
```

Lists four `.py` files.

---

## Step 3 — Create the GitHub repository

Two routes. Pick one.

### Route A — GitHub CLI (faster)

Install `gh` from cli.github.com, then:

```bash
gh auth login
```

Choose GitHub.com → HTTPS → authenticate in browser.

```bash
gh repo create traffic-hive-v2 --private --source=. --remote=origin
```

Use `--public` instead of `--private` if your panel or adviser needs to browse
it without an invite. Private is the safer default for unpublished thesis work;
you can flip it later in repo settings.

### Route B — Web interface

1. Go to github.com/new
2. Repository name: `traffic-hive-v2`
3. Leave "Add a README", `.gitignore`, and license **unchecked** — you already
   have those locally, and pre-adding them creates a conflict on first push.
4. Create repository, then connect it:

```bash
git remote add origin https://github.com/YOUR-USERNAME/traffic-hive-v2.git
```

**Check (either route):**

```bash
git remote -v
```

Should print an `origin` fetch and push URL.

---

## Step 4 — Python environment and dependencies

```bash
python -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows PowerShell
```

Your prompt should now be prefixed with `(.venv)`. Then:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This pulls `eclipse-sumo`, which bundles the SUMO binaries.

**Check:**

```bash
netconvert --version
```

Prints a SUMO version. If "command not found", the venv isn't active — activate
it and retry.

---

## Step 5 — First commit and push

Confirm the ignore rules are working before committing, so you don't push
hundreds of megabytes of simulation output:

```bash
git status --short
```

You should see the source files. You should **not** see `.venv/`, `data/out/`,
or any `.xml` or `.bin` files. If you do, `.gitignore` didn't copy over — fix
it before continuing.

```bash
git add .
git commit -m "Pipeline, server, and network builder"
git push -u origin main
```

**Check:** refresh the repo page on GitHub; your files are there.

---

## Step 6 — Add your map

```bash
cp /path/to/your/map.osm data/raw/map.osm
```

The `.gitignore` excludes `data/raw/*.osm` deliberately — OSM extracts are large
and regenerable, so they don't belong in version control. Keep a copy somewhere
backed up.

**Check:**

```bash
ls -lh data/raw/map.osm
```

---

## Step 7 — Build the network

```bash
make net
```

Or without make:

```bash
python -m pipeline.build_network --osm data/raw/map.osm
```

This prints an audit when it finishes:

```
Network audit
----------------------------------
edges                         ...
junctions                     ...
traffic_lights                ...
pedestrian_lanes              ...
crossings                     ...
elevated_edges                ...
```

### Reading the audit

| Symptom | Meaning | Action |
|---|---|---|
| `traffic_lights` is 0 or very low | OSM lacks `highway=traffic_signals` nodes | Signals need synthesizing at major junctions |
| `crossings` is 0 | OSM lacks `footway=crossing` | Either accept no crossings drawn, or synthesize |
| `edges` above ~4000 | Extract is far wider than the corridor | Re-clip with `--boundary lon,lat,lon,lat` |
| `elevated_edges` is 0 but you know there's a flyover | OSM `layer` tag missing on those ways | Flyovers will render flat; tag manually or accept |

None of these block you. They change what the map looks like, which is why it's
worth knowing before the renderer is built rather than after.

**Check:** `data/net/corridor.net.xml` exists and is more than a few KB.

---

## Step 8 — Run the scenarios

Start small to confirm the plumbing before committing to a long run:

```bash
python -m pipeline.run_scenarios --scenario actuated --vehicles 100 --horizon 120
```

That should finish in well under a minute. Then the real thing:

```bash
make runs VEHICLES=1000 HORIZON=900
```

This runs all four conditions — `fixed`, `actuated`, `ue`, `hive`. The two
routing scenarios use TraCI and are noticeably slower than the two that don't.
Expect several minutes total.

**Check:** `data/out/` contains four folders, each with `fcd.xml`,
`tripinfo.xml`, and `summary.xml`.

---

## Step 9 — Pack the assets

```bash
make pack
```

Prints one line per scenario with frame count, vehicle count, and file size,
then writes `metrics.json`.

**Check:**

```bash
ls -lh app/assets/sim/
```

You should see `net.json`, `metrics.json`, and one `traj_*.bin` per scenario.

Look at the headline numbers:

```bash
python -c "import json;print(json.dumps(json.load(open('app/assets/sim/metrics.json'))['comparison'],indent=2))"
```

Positive percentages mean the scenario beat the baseline. Negative means it
lost. Both are results — a negative number on an uncongested run is expected
behavior for HPDM, not a bug.

---

## Step 10 — Start the server

```bash
pip install fastapi uvicorn httpx
python -m server.app --serve
```

It prints a LAN address like `http://192.168.1.14:8000`. Confirm it responds:

```bash
curl http://localhost:8000/health
```

Before a defense, pre-warm the combinations you plan to show so they return
instantly instead of simulating live:

```bash
python -m server.app --prewarm 250 500 1000 2000 --durations 60 --seed 42
```

**Check:** `/health` reports a non-zero `cached_runs` after pre-warming.

---

## Step 11 — Create the Expo app

```bash
npx create-expo-app@latest app --template blank-typescript
cd app
npx expo install @shopify/react-native-skia react-native-gesture-handler \
  react-native-reanimated expo-file-system react-native-svg
npx expo install react-dom react-native-web @expo/metro-runtime
```

If `create-expo-app` refuses because `app/` already contains `assets/`, create
it elsewhere and move the generated files in:

```bash
npx create-expo-app@latest tmp-app --template blank-typescript
cp -r tmp-app/. app/
rm -rf tmp-app
```

**Check:**

```bash
npx expo start --web
```

A browser opens with the default Expo screen. That confirms the toolchain
before any custom rendering is added.

---

## Step 12 — Commit your progress

```bash
cd ..
git add .
git commit -m "Expo app scaffold and packed demo assets"
git push
```

`.gitignore` keeps `node_modules/` and the `.bin` files out. The trajectory
binaries are regenerable from the pipeline, so there's no reason to version
them.

---

## Working routine from here

```bash
source .venv/bin/activate
make all VEHICLES=1000 HORIZON=900     # rebuild everything
python -m server.app --serve           # terminal 1
cd app && npx expo start               # terminal 2
```

---

## Troubleshooting

**`netconvert: command not found`** — the venv isn't active. Run the activate
command from Step 4.

**`ModuleNotFoundError: No module named 'pipeline'`** — you're not in the repo
root. `cd` to `traffic-hive-v2` and rerun.

**`make: command not found`** on Windows — skip make and run the underlying
`python -m pipeline.<module>` commands directly; they're listed in the Makefile.

**Push rejected as non-fast-forward** — you initialised the GitHub repo with a
README. Run `git pull --rebase origin main`, then push again.

**Phone can't reach the server** — it's on cellular, or on a different wifi
network, or the laptop firewall is blocking port 8000. Campus wifi often
isolates clients from each other; a phone hotspot that the laptop joins is the
usual workaround.

**Run takes far longer than expected** — check the edge count from Step 7. A
network that wasn't clipped to the corridor can be ten times larger than it
needs to be.
