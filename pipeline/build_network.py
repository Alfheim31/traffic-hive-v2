"""Convert an OpenStreetMap extract into a SUMO network for Traffic Hive.

The flags here are not cosmetic. Each one preserves a feature the renderer
needs downstream:

  --tls.guess-signals / --tls.join   signalised junctions become <junction
                                     type="traffic_light"> with a <tlLogic>
  --crossings.guess                  emits <crossing> elements
  --sidewalks.guess                  emits lanes with allow="pedestrian"
  --osm.layer-elevation              keeps OSM layer=* so flyovers can be
                                     z-ordered against what they cross
  --remove-edges.by-vclass           strips footway/cycleway clutter that
                                     inflates the network without affecting
                                     vehicle dynamics

Usage:
    python -m pipeline.build_network --osm data/raw/map.osm
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DROP_VCLASSES = "rail,rail_urban,rail_electric,tram,ship,cable_car,subway"
KEEP_VCLASSES = "passenger,bus,truck,motorcycle,moped,taxi,delivery,emergency"


def netconvert_args(osm: Path, out: Path, boundary: str | None) -> list[str]:
    """Build the netconvert argument vector."""
    args = [
        "netconvert",
        "--osm-files", str(osm),
        "-o", str(out),
        # --- topology cleanup -------------------------------------------
        "--geometry.remove",
        "--roundabouts.guess",
        "--ramps.guess",
        "--junctions.join",
        "--junctions.join-dist", "18",
        "--no-turnarounds.tls",
        # --- signals ----------------------------------------------------
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--tls.join",
        "--tls.default-type", "actuated",
        # --- pedestrian infrastructure ----------------------------------
        "--sidewalks.guess",
        "--crossings.guess",
        "--walkingareas",
        # --- elevation / flyovers ---------------------------------------
        "--osm.layer-elevation", "4",
        # --- vclass filtering -------------------------------------------
        "--remove-edges.by-vclass", DROP_VCLASSES,
        "--keep-edges.by-vclass", KEEP_VCLASSES,
        # --- output hygiene ---------------------------------------------
        "--output.street-names", "true",
        "--output.original-names", "true",
        "--proj.utm",
        "--no-warnings",
    ]
    if boundary:
        args += ["--keep-edges.in-geo-boundary", boundary]
    return args


def build(osm: Path, out: Path, boundary: str | None = None) -> Path:
    """Run netconvert and return the path to the produced network."""
    if shutil.which("netconvert") is None:
        sys.exit("netconvert not found. Install SUMO or `pip install eclipse-sumo`.")
    if not osm.exists():
        sys.exit(f"OSM file not found: {osm}")

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = netconvert_args(osm, out, boundary)
    print("$", " ".join(cmd), "\n")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(f"netconvert failed with code {result.returncode}")
    print(result.stdout)
    return out


def audit(net_path: Path) -> dict[str, int]:
    """Count the features the renderer depends on.

    Run this immediately after building. If crossings or traffic lights come
    back as zero, the OSM extract lacks the tags and no amount of frontend
    work will surface them.
    """
    import sumolib  # imported late so --help works without SUMO tools

    net = sumolib.net.readNet(str(net_path), withPedestrianConnections=True)
    edges = net.getEdges()
    nodes = net.getNodes()

    tls_nodes = [n for n in nodes if n.getType() == "traffic_light"]
    ped_lanes = sum(
        1 for e in edges for lane in e.getLanes() if lane.allows("pedestrian")
    )
    crossings = sum(
        1 for e in edges if e.getFunction() == "crossing"
    )
    layered = sum(
        1 for e in edges
        if any(abs(c[2]) > 0.5 for c in e.getShape3D())
    ) if hasattr(edges[0], "getShape3D") else 0

    stats = {
        "edges": len(edges),
        "junctions": len(nodes),
        "traffic_lights": len(tls_nodes),
        "pedestrian_lanes": ped_lanes,
        "crossings": crossings,
        "elevated_edges": layered,
    }
    return stats


def print_audit(stats: dict[str, int]) -> None:
    print("\nNetwork audit")
    print("-" * 34)
    for key, value in stats.items():
        print(f"{key:<20} {value:>12,}")
    print("-" * 34)

    if stats["traffic_lights"] == 0:
        print("WARNING: no traffic lights. Check OSM highway=traffic_signals tags.")
    if stats["crossings"] == 0:
        print("WARNING: no crossings. OSM lacks footway=crossing in this extract.")
    if stats["edges"] > 4000:
        print("WARNING: network is large for a corridor study. Clip the boundary.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", type=Path, default=Path("data/raw/map.osm"))
    parser.add_argument("--out", type=Path, default=Path("data/net/corridor.net.xml"))
    parser.add_argument(
        "--boundary",
        default=None,
        help="lon,lat,lon,lat bounding box to clip to the corridor",
    )
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()

    net = args.out if args.audit_only else build(args.osm, args.out, args.boundary)
    print_audit(audit(net))


if __name__ == "__main__":
    main()
