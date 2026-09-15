"""Pack SUMO outputs into assets the React Native app can render directly.

Produces three artefacts per build:

  net.json            road geometry, signal positions, crossings, layer
                      ordering, all in a local metre grid with the origin at
                      the network centre
  traj_<scen>.bin     int16 quantised vehicle positions, one frame per FCD
                      sample, little-endian
  metrics.json        precomputed Chapter 4 statistics for every scenario

The binary layout is deliberately flat so the app can memory-map it and
index frames arithmetically rather than parsing anything at runtime.

Binary format (little-endian)
-----------------------------
  magic        4 bytes   b"THV1"
  n_frames     uint32
  n_vehicles   uint32
  scale        float32   metres per quantisation unit
  frame_dt     float32   seconds between frames
  types        uint8  * n_vehicles       0=car 1=jeepney 2=motorcycle
  frames       n_frames * n_vehicles * (int16 x, int16 y, uint8 speed_pct,
                                        uint8 flags)

A vehicle not present in a frame is encoded with x = y = INT16_MIN.

Usage:
    python -m pipeline.pack --scenarios fixed actuated ue hive
"""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from pipeline import theory

NET = Path("data/net/corridor.net.xml")
OUT = Path("data/out")
APP_ASSETS = Path("app/assets/sim")

ABSENT = -32768
TYPE_CODES = {"car": 0, "jeepney": 1, "motorcycle": 2}


# --------------------------------------------------------------------------
# Network geometry
# --------------------------------------------------------------------------

@dataclass
class NetBounds:
    """Local metre grid definition, origin at network centre."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def cx(self) -> float:
        return (self.min_x + self.max_x) / 2.0

    @property
    def cy(self) -> float:
        return (self.min_y + self.max_y) / 2.0

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def scale_for_int16(self) -> float:
        """Metres per quantisation unit that keeps the network inside int16.

        int16 spans +/-32767. Using the half-extent of the larger axis plus
        a 5% margin keeps every coordinate representable with sub-decimetre
        resolution on any network smaller than about 6 km across.
        """
        half = max(self.width, self.height) / 2.0 * 1.05
        return max(half / 32000.0, 1e-6)


def export_network(net_path: Path) -> dict:
    """Extract renderable geometry from a SUMO network.

    Everything is emitted in the local metre grid. Edges carry a `layer`
    derived from their mean z so the app can z-order flyovers above the
    roads they cross rather than drawing a false intersection.
    """
    import sumolib

    net = sumolib.net.readNet(str(net_path), withInternal=False)
    xmin, ymin, xmax, ymax = net.getBoundary()
    bounds = NetBounds(xmin, ymin, xmax, ymax)
    cx, cy = bounds.cx, bounds.cy

    roads: list[dict] = []
    crossings: list[dict] = []

    for edge in net.getEdges():
        shape = edge.getShape()
        if len(shape) < 2:
            continue
        pts = [round(p[0] - cx, 2) for p in shape], [round(p[1] - cy, 2) for p in shape]
        flat = [c for pair in zip(pts[0], pts[1]) for c in pair]

        try:
            shape3d = edge.getShape3D()
            zs = [p[2] for p in shape3d if len(p) > 2]
            layer = int(round(sum(zs) / len(zs) / 4.0)) if zs else 0
        except Exception:
            layer = 0

        is_ped = all(lane.allows("pedestrian") and not lane.allows("passenger")
                     for lane in edge.getLanes())
        function = edge.getFunction()

        if function == "crossing" or is_ped:
            crossings.append({"p": flat, "w": is_ped})
            continue

        roads.append({
            "id": edge.getID(),
            "p": flat,
            "n": len(edge.getLanes()),
            "s": round(edge.getSpeed(), 1),
            "l": layer,
            "name": edge.getName() or "",
        })

    # Crossings are internal edges, which sumolib omits by default. Scan the
    # raw network for function="crossing" so pedestrian infrastructure the
    # panel expects to see actually reaches the renderer.
    root = ET.parse(net_path).getroot()
    for edge_el in root.findall("edge"):
        if edge_el.get("function") != "crossing":
            continue
        for lane_el in edge_el.findall("lane"):
            raw = lane_el.get("shape")
            if not raw:
                continue
            coords: list[float] = []
            for pair in raw.split():
                parts = pair.split(",")
                coords.append(round(float(parts[0]) - cx, 2))
                coords.append(round(float(parts[1]) - cy, 2))
            if len(coords) >= 4:
                crossings.append({"p": coords, "w": False})
            break

    lights: list[dict] = []
    for node in net.getNodes():
        if node.getType() != "traffic_light":
            continue
        x, y = node.getCoord()
        lights.append({
            "id": node.getID(),
            "x": round(x - cx, 2),
            "y": round(y - cy, 2),
        })

    return {
        "version": 1,
        "bounds": {
            "w": round(bounds.width, 2),
            "h": round(bounds.height, 2),
        },
        "scale": bounds.scale_for_int16(),
        "roads": roads,
        "crossings": crossings,
        "lights": lights,
        "counts": {
            "roads": len(roads),
            "crossings": len(crossings),
            "lights": len(lights),
        },
    }


# --------------------------------------------------------------------------
# Trajectories
# --------------------------------------------------------------------------

def pack_trajectories(
    fcd_path: Path,
    out_path: Path,
    centre: tuple[float, float],
    scale: float,
    max_speed: float = 30.0,
) -> dict:
    """Stream an FCD file into the flat binary format.

    Uses iterparse and clears each timestep element after processing, so
    peak memory stays proportional to one frame rather than to the whole
    file. A 15-minute 1000-vehicle FCD dump is several hundred megabytes of
    XML; holding it in a DOM is not viable.
    """
    cx, cy = centre
    ids: dict[str, int] = {}
    types: list[int] = []
    frames: list[dict[int, tuple[int, int, int, int]]] = []
    times: list[float] = []

    context = ET.iterparse(str(fcd_path), events=("end",))
    for _, elem in context:
        if elem.tag != "timestep":
            continue
        t = float(elem.get("time", "0"))
        frame: dict[int, tuple[int, int, int, int]] = {}
        for veh in elem.findall("vehicle"):
            vid = veh.get("id")
            if vid not in ids:
                ids[vid] = len(ids)
                vtype = (veh.get("type") or "car").split("@")[0]
                types.append(TYPE_CODES.get(vtype, 0))
            idx = ids[vid]
            x = int(round((float(veh.get("x")) - cx) / scale))
            y = int(round((float(veh.get("y")) - cy) / scale))
            speed = float(veh.get("speed", "0"))
            pct = max(0, min(255, int(round(speed / max_speed * 255))))
            # flags bit 0: stopped (below 0.3 m/s) -> renderer colours red
            flags = 1 if speed < 0.3 else 0
            frame[idx] = (
                max(-32767, min(32767, x)),
                max(-32767, min(32767, y)),
                pct,
                flags,
            )
        frames.append(frame)
        times.append(t)
        elem.clear()

    n_frames = len(frames)
    n_veh = len(ids)
    if n_frames == 0 or n_veh == 0:
        raise SystemExit(f"no vehicle data found in {fcd_path}")

    frame_dt = (times[-1] - times[0]) / max(1, n_frames - 1) if n_frames > 1 else 1.0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as fh:
        fh.write(b"THV1")
        fh.write(struct.pack("<IIff", n_frames, n_veh, scale, frame_dt))
        fh.write(bytes(types))
        blank = struct.pack("<hhBB", ABSENT, ABSENT, 0, 0)
        for frame in frames:
            row = bytearray()
            for i in range(n_veh):
                rec = frame.get(i)
                row += blank if rec is None else struct.pack("<hhBB", *rec)
            fh.write(row)

    size = out_path.stat().st_size
    return {
        "file": out_path.name,
        "frames": n_frames,
        "vehicles": n_veh,
        "frame_dt": round(frame_dt, 3),
        "duration_s": round(times[-1] - times[0], 1),
        "bytes": size,
        "mb": round(size / 1_048_576, 2),
    }


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def compute_metrics(tripinfo_path: Path, summary_path: Path) -> dict:
    """Derive the Chapter 4 statistics from a completed run.

    Computed here, once, so the number the panel sees on screen is the same
    number that appears in the paper. The app never recomputes these.
    """
    durations: list[float] = []
    delays: list[float] = []
    waits: list[float] = []
    time_loss: list[float] = []
    routes: list[float] = []

    for _, elem in ET.iterparse(str(tripinfo_path), events=("end",)):
        if elem.tag != "tripinfo":
            continue
        durations.append(float(elem.get("duration", 0)))
        delays.append(float(elem.get("departDelay", 0)))
        waits.append(float(elem.get("waitingTime", 0)))
        time_loss.append(float(elem.get("timeLoss", 0)))
        routes.append(float(elem.get("routeLength", 0)))
        elem.clear()

    series_t: list[float] = []
    series_running: list[int] = []
    series_halting: list[int] = []
    inserted_total = 0
    ended_total = 0
    for _, elem in ET.iterparse(str(summary_path), events=("end",)):
        if elem.tag != "step":
            continue
        series_t.append(float(elem.get("time", 0)))
        series_running.append(int(elem.get("running", 0)))
        series_halting.append(int(elem.get("halting", 0)))
        inserted_total = max(inserted_total, int(elem.get("inserted", 0)))
        ended_total = max(ended_total, int(elem.get("ended", 0)))
        elem.clear()

    # Completion accounting.
    #
    # Trip-level averages are computed over vehicles that finished. If one
    # scenario strands more vehicles than another, its slowest trips are
    # silently excluded and its averages look better than they are — a
    # survivorship bias that can invert the apparent ranking. Recording the
    # completion rate makes that visible instead of letting it hide inside
    # a mean.
    stranded = max(0, inserted_total - ended_total)
    completion_rate = (
        float(ended_total) / inserted_total if inserted_total else 1.0
    )

    n = max(1, len(durations))
    mean = lambda xs: sum(xs) / max(1, len(xs))

    # Synchronisation and fairness. The manuscript's synchronisation term is
    # the variance of arrival times; it is reported here as a measured
    # quantity so its behaviour can be inspected directly rather than
    # inferred. The fairness ratio is the complementary view: how much worse
    # the unluckiest decile fares than the median, which is what the
    # constrained formulation bounds and what variance alone cannot express.
    dur = np.array(durations, dtype=float) if durations else np.array([0.0])
    duration_std = float(np.std(dur))
    p50 = float(np.percentile(dur, 50))
    p90 = float(np.percentile(dur, 90))
    p95 = float(np.percentile(dur, 95))
    fairness_ratio = float(p90 / p50) if p50 > 0 else 1.0
    # Coefficient of variation is scale-free, so it can be compared across
    # runs of different durations in a way that raw variance cannot.
    cv = float(duration_std / np.mean(dur)) if float(np.mean(dur)) > 0 else 0.0

    # Downsample the time series to at most 120 points for charting.
    stride = max(1, len(series_t) // 120)
    chart_t = series_t[::stride]
    chart_halting = series_halting[::stride]
    chart_running = series_running[::stride]

    return {
        "completed_trips": len(durations),
        "inserted": inserted_total,
        "stranded": stranded,
        "completion_rate": round(completion_rate, 4),
        "duration_std_s": round(duration_std, 3),
        "duration_p50_s": round(p50, 2),
        "duration_p90_s": round(p90, 2),
        "duration_p95_s": round(p95, 2),
        "arrival_variance": round(duration_std**2, 3),
        "fairness_ratio_p90_p50": round(fairness_ratio, 4),
        "coefficient_of_variation": round(cv, 5),
        "avg_duration_s": round(mean(durations), 2),
        "avg_time_loss_s": round(mean(time_loss), 2),
        "avg_waiting_s": round(mean(waits), 2),
        "avg_depart_delay_s": round(mean(delays), 2),
        "avg_route_length_m": round(mean(routes), 1),
        "total_time_loss_h": round(sum(time_loss) / 3600.0, 3),
        "peak_halting": max(series_halting) if series_halting else 0,
        "series": {
            "t": [round(v, 1) for v in chart_t],
            "halting": chart_halting,
            "running": chart_running,
        },
    }


def compare(metrics: dict[str, dict], baseline: str = "ue") -> dict:
    """Percentage improvements of every scenario against a baseline."""
    if baseline not in metrics:
        baseline = next(iter(metrics))
    base = metrics[baseline]
    out: dict[str, dict] = {}
    base_rate = base.get("completion_rate", 1.0)
    for name, m in metrics.items():
        def pct(key: str) -> float:
            b = base[key]
            if not b:
                return 0.0
            return round((b - m[key]) / b * 100.0, 2)

        out[name] = {
            "vs": baseline,
            "time_loss_reduction_pct": pct("avg_time_loss_s"),
            "waiting_reduction_pct": pct("avg_waiting_s"),
            "duration_reduction_pct": pct("avg_duration_s"),
            "spread_reduction_pct": pct("duration_std_s"),
            "fairness_improvement_pct": pct("fairness_ratio_p90_p50"),
            # True when both scenarios cleared a comparable share of their
            # demand. When false, the trip averages above are computed over
            # different vehicle populations and are not directly comparable;
            # the app surfaces this rather than hiding it.
            "comparable": bool(
                abs(m.get("completion_rate", 1.0) - base_rate) <= 0.02
            ),
            "completion_rate": m.get("completion_rate", 1.0),
            "queue_reduction_pct": (
                round((base["peak_halting"] - m["peak_halting"])
                      / base["peak_halting"] * 100.0, 2)
                if base["peak_halting"] else 0.0
            ),
        }
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--net", type=Path, default=NET)
    parser.add_argument("--scenarios", nargs="+", default=["actuated", "hive"])
    parser.add_argument("--baseline", default="ue")
    parser.add_argument("--assets", type=Path, default=APP_ASSETS)
    args = parser.parse_args()

    import sumolib

    net = sumolib.net.readNet(str(args.net))
    xmin, ymin, xmax, ymax = net.getBoundary()
    centre = ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)

    print("exporting network geometry...")
    net_json = export_network(args.net)
    scale = net_json["scale"]
    args.assets.mkdir(parents=True, exist_ok=True)
    (args.assets / "net.json").write_text(json.dumps(net_json, separators=(",", ":")))
    print(f"  net.json  {net_json['counts']}  "
          f"{(args.assets / 'net.json').stat().st_size / 1024:.0f} KB")

    manifest: dict[str, dict] = {}
    metrics: dict[str, dict] = {}

    for name in args.scenarios:
        run_dir = OUT / name
        fcd = run_dir / "fcd.xml"
        if not fcd.exists():
            print(f"  skipping {name}: no fcd.xml")
            continue
        print(f"packing {name}...")
        info = pack_trajectories(fcd, args.assets / f"traj_{name}.bin", centre, scale)
        manifest[name] = info
        print(f"  {info['file']}  {info['frames']} frames x {info['vehicles']} veh"
              f"  {info['mb']} MB")
        metrics[name] = compute_metrics(run_dir / "tripinfo.xml",
                                        run_dir / "summary.xml")

    # Analytical reference values. These come from exhaustive enumeration
    # over the corridor model rather than from simulation, and are carried
    # alongside the measured results so the app can show what the measured
    # numbers should be compared against: the unreachable System Optimum
    # below, the selfish User Equilibrium above, and the Price of Anarchy
    # bounding the gap between them.
    analytical = theory.summarise(
        theory.default_corridors(), demand=10, epsilon=0.25
    )

    payload = {
        "scenarios": manifest,
        "metrics": metrics,
        "comparison": compare(metrics, args.baseline) if metrics else {},
        "theory": analytical,
    }
    (args.assets / "metrics.json").write_text(json.dumps(payload, separators=(",", ":")))
    print(f"\nmetrics.json written "
          f"({(args.assets / 'metrics.json').stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
