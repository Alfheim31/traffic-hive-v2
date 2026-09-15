# Traffic Hive v2 — setup walkthrough (GitHub Desktop + VS Code)

This version avoids terminal navigation entirely during setup. GitHub Desktop
decides where the folder lives, VS Code opens that exact folder, and its
built-in terminal starts inside it — so there is never a question of which
directory you are in.

Written for macOS. Estimated time: 30–45 minutes, mostly installs.

---

## Step 0 — Clear out the earlier attempt

There is a half-made `traffic-hive-v2` folder in your home directory from the
previous attempt. Remove it so GitHub Desktop doesn't refuse to clone into an
existing path.

Open Finder, press `Cmd + Shift + H` for your home folder, find
`traffic-hive-v2`, drag it to the Trash.

Terminal equivalent:

```bash
rm -rf ~/traffic-hive-v2
```

**Check:** the folder is gone from your home directory.

---

## Step 1 — Create the repository on GitHub

1. Go to **github.com/new**
2. **Repository name:** `traffic-hive-v2`
3. **Description:** optional, e.g. "Swarm routing simulation and defense demo"
4. **Visibility:** Private is the safer default for unpublished thesis work.
   You can switch to Public later in Settings.
5. **Leave every initialisation box unchecked** — no README, no `.gitignore`,
   no license. The zip already contains both, and letting GitHub create its own
   is the most common cause of a failed first push.
6. Click **Create repository**

**Check:** you land on a page showing setup instructions for an empty repo.

---

## Step 2 — Install and sign in to GitHub Desktop

1. Download from **desktop.github.com**
2. Open it and sign in with your GitHub account
3. Let it configure your name and email for commits when prompted

**Check:** the app shows your username in the top-left area.

---

## Step 3 — Clone the repository

In GitHub Desktop:

1. **File → Clone repository** (`Cmd + Shift + O`)
2. Pick the **GitHub.com** tab
3. Select `traffic-hive-v2`. If it isn't listed, click the refresh icon.
4. **Note the Local Path** — usually
   `/Users/YOUR-NAME/Documents/GitHub/traffic-hive-v2`. This is now the one and
   only location of your project.
5. Click **Clone**

Desktop will say the repository is empty. Expected.

**Check:** Desktop shows `traffic-hive-v2` as the current repository with "No
local changes".

---

## Step 4 — Open it in VS Code

1. Install VS Code from **code.visualstudio.com** if needed
2. In GitHub Desktop, click **Open in Visual Studio Code**

If that button is greyed out, open VS Code manually, then **File → Open
Folder** and choose the Local Path from Step 3.

Install the Python extension too: `Cmd + Shift + X`, search "Python", install
the Microsoft one.

**Check:** VS Code's Explorer shows `TRAFFIC-HIVE-V2` with nothing under it.

---

## Step 5 — Put the project files in

1. In GitHub Desktop: **Repository → Show in Finder** (`Cmd + Shift + F`). This
   opens the exact right folder, no guessing.
2. In a second Finder window, open your unzipped `traffic-hive-v2` folder from
   Downloads.
3. **Press `Cmd + Shift + .`** in that window to reveal hidden files. You need
   this — `.gitignore` starts with a dot and is invisible otherwise. Without
   it, your first commit will include your virtual environment and hundreds of
   megabytes of simulation output.
4. `Cmd + A` to select everything inside the Downloads folder, including
   `.gitignore`, and drag it into the GitHub folder from step 1.

You are copying the *contents*, not the folder itself. If you end up with
`traffic-hive-v2/traffic-hive-v2/`, open the inner folder, select all, move it
up a level, delete the empty inner folder.

**Check:** VS Code's Explorer now shows `pipeline`, `server`, `data`, `app`,
`Makefile`, `README.md`, `SETUP.md`, `requirements.txt`, `.gitignore`.

Also check GitHub Desktop — it should list around 15 changed files. If it shows
thousands, `.gitignore` didn't come across; repeat step 3.

---

## Step 6 — Create the Python environment

In VS Code, open the terminal with **`Ctrl + ~`** (Control, not Command). It
opens already inside your project folder — this is the entire reason for the
GUI route.

```bash
pwd
ls
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`.

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

A few minutes; `eclipse-sumo` is a large download.

**Check:**

```bash
netconvert --version
```

Prints a SUMO version. If "command not found", the venv isn't active — rerun
the activate line.

> You have an existing `~/Sumo` install. It won't conflict — the venv's copy
> takes priority while activated, so the version printed here may differ from
> your system one. That's fine.

Point VS Code at it too: `Cmd + Shift + P` → "Python: Select Interpreter" →
choose the one under `.venv`.

---

## Step 7 — First commit and push

Back in GitHub Desktop:

1. Confirm the changed-files list looks like source code — not `.venv`, not
   `node_modules`, not `.xml`. If `.venv` appears, stop and fix `.gitignore`.
2. Summary: `Pipeline, server, and network builder`
3. **Commit to main**
4. **Push origin**

**Check:** refresh your repo page on GitHub; the files are there.

---

## Step 8 — Add your map

**Repository → Show in Finder**, open `data/raw`, copy your `map.osm` in.

`map.osm` is deliberately excluded from git — OSM extracts are large and
regenerable. Keep a backup copy elsewhere.

**Check** in the VS Code terminal:

```bash
ls -lh data/raw/map.osm
```

---

## Step 9 — Build the network

```bash
make net
```

If `make` isn't available:

```bash
python -m pipeline.build_network --osm data/raw/map.osm
```

An audit prints when it finishes:

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
| `crossings` is 0 | OSM lacks `footway=crossing` | Accept none drawn, or synthesize |
| `edges` above ~4000 | Extract much wider than the corridor | Re-clip with `--boundary lon,lat,lon,lat` |
| `elevated_edges` is 0 despite a known flyover | OSM `layer` tag missing | Flyovers render flat unless tagged |

None of these block progress. They determine what the map looks like, which is
why it's worth knowing before the renderer is built rather than after.

**Check:** `data/net/corridor.net.xml` appears in the Explorer panel.

---

## Step 10 — Test run

Small first:

```bash
python -m pipeline.run_scenarios --scenario actuated --vehicles 100 --horizon 120
```

Under a minute. Then the real one:

```bash
make runs VEHICLES=1000 HORIZON=900
```

The two routing scenarios use TraCI and are noticeably slower than the two that
don't. Expect several minutes.

**Check:** `data/out/` holds four folders, each with `fcd.xml`, `tripinfo.xml`,
`summary.xml`.

---

## Step 11 — Pack the assets

```bash
make pack
```

Then the headline comparison:

```bash
python -c "import json;print(json.dumps(json.load(open('app/assets/sim/metrics.json'))['comparison'],indent=2))"
```

Positive percentages mean the scenario beat the baseline; negative means it
lost. Both are results. A negative figure on an uncongested run is expected
behavior for HPDM, not a bug.

**Check:** `app/assets/sim/` contains `net.json`, `metrics.json`, and one
`traj_*.bin` per scenario.

---

## Step 12 — Start the server

```bash
python -m server.app --serve
```

It prints a LAN address. Open a **second** terminal with the `+` icon in the
terminal panel, and test:

```bash
curl http://localhost:8000/health
```

Before a defense, pre-warm what you plan to show so it returns instantly rather
than simulating live:

```bash
python -m server.app --prewarm 250 500 1000 2000 --durations 60 --seed 42
```

---

## Step 13 — Create the Expo app

```bash
npx create-expo-app@latest tmp-app --template blank-typescript
cp -r tmp-app/. app/
rm -rf tmp-app
cd app
npx expo install @shopify/react-native-skia react-native-gesture-handler \
  react-native-reanimated expo-file-system react-native-svg
npx expo install react-dom react-native-web @expo/metro-runtime
```

The temp-folder detour is because `create-expo-app` refuses to scaffold into
`app/` once the packer has put `assets/sim/` there.

**Check:**

```bash
npx expo start --web
```

A browser opens with the default Expo screen, confirming the toolchain before
any custom rendering is added. Then `cd ..` to return to the project root.

---

## Daily routine

Open VS Code from GitHub Desktop, then:

```bash
source .venv/bin/activate
python -m server.app --serve          # terminal 1
cd app && npx expo start              # terminal 2
```

Commit through GitHub Desktop rather than the terminal — you get to see exactly
what is being included before it goes in.

---

## Troubleshooting

**`netconvert: command not found`** — venv not active. `source .venv/bin/activate`.

**`ModuleNotFoundError: No module named 'pipeline'`** — wrong directory. In VS
Code, `Cmd + Shift + P` → "Terminal: Create New Terminal" opens at the project
root.

**GitHub Desktop shows thousands of changed files** — `.gitignore` missing or
not copied. Check with `ls -a`, re-copy with hidden files visible
(`Cmd + Shift + .` in Finder).

**`make: command not found`** — use the `python -m pipeline.<module>` commands
directly; they're all in the Makefile.

**Push rejected** — commits exist on GitHub that you don't have locally. In
Desktop, **Pull origin** first, then push.

**Phone can't reach the server** — different wifi, cellular data, or campus
wifi isolating clients. A phone hotspot the laptop joins is the usual
workaround.

---

*A terminal-only version of this walkthrough is in `SETUP-cli.md`.*
