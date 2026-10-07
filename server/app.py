"""Local simulation server for the Traffic Hive demo app.

The app posts a vehicle count and a duration; this runs the scenarios, packs
the assets, and serves them back. Completed runs are cached on disk keyed by
their parameters, so a combination that has been run before returns
immediately.

The intended pattern for a defense: pre-warm the combinations you plan to
demonstrate so they are cache hits and respond instantly, while leaving the
live path available for anything a panellist asks for on the spot.

    python -m server.app --prewarm 500 1000 --durations 60

Run:
    uvicorn server.app:api --host 0.0.0.0 --port 8000

The host binding matters: 0.0.0.0 makes the server reachable from a phone on
the same network. Print the LAN address at startup and point the app at it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import socket
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import pack, run_scenarios
from pipeline.run_scenarios import SCENARIOS

NET = Path("data/net/corridor.net.xml")
CACHE = Path("data/cache")
HIVE_OUT = Path(__file__).resolve().parent.parent / "hive_out"
RunState = Literal["queued", "running", "done", "error"]


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------

class RunRequest(BaseModel):
    """Parameters the app collects from the two input fields."""

    vehicles: int = Field(default=1000, ge=10, le=20000)
    duration_s: int = Field(default=60, ge=10, le=3600)
    seed: int = Field(default=42, ge=0, le=99999)
    scenarios: list[str] = Field(default_factory=lambda: ["ue", "hive"])
    baseline: str = "ue"
    # Roadside obstacles applied inside SUMO (see pipeline/obstacles.py).
    obstacles: list[dict] = Field(default_factory=list, max_length=200)
    # Seconds between trajectory frames. 1.0 halves the asset size of long
    # runs; the Hive Out view interpolates between frames either way.
    fcd_period: float = Field(default=0.5, ge=0.25, le=2.0)

    def key(self) -> str:
        """Stable cache key for this parameter set."""
        payload = json.dumps(
            {
                "v": self.vehicles,
                "d": self.duration_s,
                "s": self.seed,
                "sc": sorted(self.scenarios),
                "b": self.baseline,
                "o": self.obstacles,
                "f": self.fcd_period,
            },
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()[:16]


@dataclass
class RunRecord:
    """Server-side status of one simulation request."""

    key: str
    state: RunState
    progress: float
    message: str
    params: dict
    started_at: float
    finished_at: float | None = None
    error: str | None = None

    def public(self) -> dict:
        data = asdict(self)
        if self.state == "done":
            data["assets"] = {
                "net": f"/assets/{self.key}/net.json",
                "metrics": f"/assets/{self.key}/metrics.json",
            }
        return data


RUNS: dict[str, RunRecord] = {}


# --------------------------------------------------------------------------
# Simulation execution
# --------------------------------------------------------------------------

def _cache_dir(key: str) -> Path:
    return CACHE / key


def _is_cached(key: str) -> bool:
    d = _cache_dir(key)
    return (d / "metrics.json").exists() and (d / "net.json").exists()


def _execute(req: RunRequest, record: RunRecord) -> None:
    """Run every requested scenario, then pack. Blocking; runs in a thread.

    The SUMO horizon is deliberately longer than the requested playback
    duration. Vehicles entering near the end of the window still need time
    to clear, and truncating the run would bias the completion statistics
    toward whichever rule happens to dispatch vehicles earlier.
    """
    import sumolib

    out_dir = _cache_dir(record.key)
    out_dir.mkdir(parents=True, exist_ok=True)

    horizon = req.duration_s
    total = len(req.scenarios)

    try:
        for i, name in enumerate(req.scenarios):
            if name not in SCENARIOS:
                raise ValueError(f"unknown scenario: {name}")
            record.state = "running"
            record.message = f"simulating {SCENARIOS[name].label}"
            record.progress = i / (total + 1)
            run_scenarios.run(
                SCENARIOS[name],
                n_vehicles=req.vehicles,
                horizon=horizon,
                seed=req.seed,
                net_path=NET,
                fcd_period=req.fcd_period,
                obstacles=req.obstacles or None,
            )

        record.message = "packing assets"
        record.progress = total / (total + 1)

        net = sumolib.net.readNet(str(NET))
        xmin, ymin, xmax, ymax = net.getBoundary()
        centre = ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)

        net_json = pack.export_network(NET)
        (out_dir / "net.json").write_text(json.dumps(net_json, separators=(",", ":")))

        manifest: dict[str, dict] = {}
        metrics: dict[str, dict] = {}
        for name in req.scenarios:
            run_dir = pack.OUT / name
            manifest[name] = pack.pack_trajectories(
                run_dir / "fcd.xml",
                out_dir / f"traj_{name}.bin",
                centre,
                net_json["scale"],
            )
            metrics[name] = pack.compute_metrics(
                run_dir / "tripinfo.xml", run_dir / "summary.xml"
            )
            # Signal states, trip metadata and resolved obstacles for the
            # Hive Out driver view.
            manifest[name]["driver"] = pack.pack_driver_data(run_dir, NET, out_dir, name)

        # How evenly each rule spread traffic, and how corridor-based rules
        # split demand: what the Hive Out results view compares.
        from pipeline.evaluate import network_spread
        spread, assignment = {}, {}
        for name in req.scenarios:
            run_dir = pack.OUT / name
            spread[name] = network_spread(run_dir)
            if (run_dir / "assignment.json").exists():
                a = json.loads((run_dir / "assignment.json").read_text())
                assignment[name] = {"share": a.get("share"), "spread_entropy": a.get("spread_entropy")}

        payload = {
            "params": req.model_dump(),
            "labels": {n: SCENARIOS[n].label for n in req.scenarios},
            "scenarios": manifest,
            "metrics": metrics,
            "comparison": pack.compare(metrics, req.baseline),
            "spread": spread,
            "assignment": assignment,
        }
        (out_dir / "metrics.json").write_text(json.dumps(payload, separators=(",", ":")))

        record.state = "done"
        record.progress = 1.0
        record.message = "complete"
        record.finished_at = time.time()

    except Exception as exc:  # surfaced to the app rather than swallowed
        record.state = "error"
        record.error = str(exc)
        record.message = "failed"
        record.finished_at = time.time()
        shutil.rmtree(out_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

api = FastAPI(title="Traffic Hive simulation server")
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.get("/health")
def health() -> dict:
    """Liveness check plus the scenarios this server can run."""
    return {
        "ok": True,
        "network": NET.exists(),
        "scenarios": {n: s.label for n, s in SCENARIOS.items()},
        "cached_runs": len([p for p in CACHE.glob("*") if p.is_dir()]) if CACHE.exists() else 0,
    }


@api.post("/run")
async def start_run(req: RunRequest) -> JSONResponse:
    """Start a simulation, or return immediately if it is already cached."""
    key = req.key()

    if _is_cached(key):
        record = RUNS.get(key) or RunRecord(
            key=key,
            state="done",
            progress=1.0,
            message="cached",
            params=req.model_dump(),
            started_at=time.time(),
            finished_at=time.time(),
        )
        record.state = "done"
        record.message = "cached"
        record.progress = 1.0
        RUNS[key] = record
        return JSONResponse({"cached": True, **record.public()})

    existing = RUNS.get(key)
    if existing and existing.state in ("queued", "running"):
        return JSONResponse({"cached": False, **existing.public()})

    record = RunRecord(
        key=key,
        state="queued",
        progress=0.0,
        message="queued",
        params=req.model_dump(),
        started_at=time.time(),
    )
    RUNS[key] = record
    asyncio.get_running_loop().run_in_executor(None, _execute, req, record)
    return JSONResponse({"cached": False, **record.public()})


@api.get("/status/{key}")
def status(key: str) -> dict:
    """Poll a run. The app drives its progress bar from this."""
    record = RUNS.get(key)
    if record is None:
        if _is_cached(key):
            return {"key": key, "state": "done", "progress": 1.0, "message": "cached"}
        raise HTTPException(404, "unknown run key")
    return record.public()


@api.get("/assets/{key}/{filename}")
def asset(key: str, filename: str) -> FileResponse:
    """Serve a packed artefact. Binaries stream without being loaded."""
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "bad filename")
    path = _cache_dir(key) / filename
    if not path.exists():
        raise HTTPException(404, f"no such asset: {filename}")
    media = "application/json" if filename.endswith(".json") else "application/octet-stream"
    return FileResponse(path, media_type=media)


@api.get("/runs")
def list_runs() -> dict:
    """Every cached run, for the app's 'recent runs' picker."""
    entries = []
    if CACHE.exists():
        for d in sorted(CACHE.glob("*")):
            meta = d / "metrics.json"
            if not meta.exists():
                continue
            try:
                params = json.loads(meta.read_text()).get("params", {})
            except json.JSONDecodeError:
                continue
            entries.append({
                "key": d.name,
                "params": params,
                "mtime": meta.stat().st_mtime,
                "driver": any(d.glob("signals_*.bin")),
            })
    return {"runs": entries}


# The Hive Out driver view, served from the same origin so it can start runs
# and load their assets. Open http://<host>:8000/hive-out/
if HIVE_OUT.exists():
    api.mount("/hive-out", StaticFiles(directory=str(HIVE_OUT), html=True), name="hive-out")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def lan_address(port: int) -> str:
    """Best-effort LAN address so the phone knows where to connect."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        host = sock.getsockname()[0]
    except OSError:
        host = "127.0.0.1"
    finally:
        sock.close()
    return f"http://{host}:{port}"


def prewarm(vehicle_counts: list[int], durations: list[int], seed: int) -> None:
    """Populate the cache before a demo so the planned runs are instant."""
    for v in vehicle_counts:
        for d in durations:
            req = RunRequest(vehicles=v, duration_s=d, seed=seed)
            key = req.key()
            if _is_cached(key):
                print(f"  cached already: {v} vehicles / {d}s")
                continue
            print(f"  warming: {v} vehicles / {d}s -> {key}")
            record = RunRecord(key, "queued", 0.0, "", req.model_dump(), time.time())
            RUNS[key] = record
            _execute(req, record)
            print(f"    {record.state} ({record.error or 'ok'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prewarm", nargs="*", type=int, default=[])
    parser.add_argument("--durations", nargs="*", type=int, default=[60])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()

    if args.prewarm:
        print("pre-warming cache...")
        prewarm(args.prewarm, args.durations, args.seed)

    if args.serve:
        import uvicorn

        print(f"\nserving on {lan_address(args.port)}")
        uvicorn.run(api, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
