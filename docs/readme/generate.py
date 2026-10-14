"""Draws every animated SVG used by the repository README.

Run from the repository root:

    python docs/readme/generate.py

Everything here is drawn from files already in the repo, so the pictures stay
honest when the numbers change:

  app/assets/sim/net.json      real España network geometry (roads, signals)
  data/net/corridors.json      the precomputed corridors HPDM chooses between
  app/assets/sim/metrics.json  queue series + the analytical three-corridor model
  RESULTS (below)              the 5-seed table printed by `python -m pipeline.evaluate`

The visual language (dark card, honeycomb, gold glow, animated cars) matches
the profile README at github.com/Alfheim31/Alfheim31.
"""
import json
import math
import os
import random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "docs", "readme")
os.makedirs(OUT, exist_ok=True)

NET = json.load(open(os.path.join(ROOT, "app/assets/sim/net.json")))
CORR = json.load(open(os.path.join(ROOT, "data/net/corridors.json")))
MET = json.load(open(os.path.join(ROOT, "app/assets/sim/metrics.json")))

# The OD pair drawn in the "two rules" card: its three corridors are the most
# visibly separate on the map. Override with SHOWCASE=<group index>.
SHOWCASE = int(os.environ.get("SHOWCASE", 7))

# Replicated evaluation, España, 1000 vehicles, 15-minute demand window, 5 seeds.
# Update these after re-running `python -m pipeline.evaluate`.
RESULTS = [
    # label, UE, Hive, unit, lower_is_better, change as printed by evaluate (None = compute)
    ("Time loss per trip", 244, 216, "s", True, -11),
    ("Waiting per trip", 115, 97, "s", True, -15),
    ("Peak queue", 236, 202, "veh", True, -15),
    ("Gini of segment load", 0.783, 0.734, "", True, None),
    ("Busiest 10% carry", 67.5, 61.3, "%", True, None),
    ("Corridor split entropy", 0.74, 0.90, "", False, None),
]

FONT = "'SF Mono','JetBrains Mono','Cascadia Code',Consolas,'Liberation Mono',Menlo,monospace"
SANS = "-apple-system,'Segoe UI','Helvetica Neue',Arial,sans-serif"
GOLD = "#F9AB00"; HONEY = "#FFD166"; RED = "#EA4335"; BLUE = "#4285F4"; GREEN = "#34A853"
BG = "#0D1117"; PANEL = "#161B22"; LINE = "#30363D"; MUTED = "#8B949E"; TEXT = "#E6EDF3"; ROAD = "#1C2230"


# ------------------------------------------------------------------ helpers
def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def hexpts(cx, cy, r):
    return " ".join(
        f"{cx + r*math.cos(math.radians(60*i+30)):.1f},{cy + r*math.sin(math.radians(60*i+30)):.1f}"
        for i in range(6))


def svg_open(w, h, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">\n'
            f'<title>{esc(title)}</title>\n')


def common_defs():
    return f'''<defs>
  <pattern id="honey" width="34.64" height="60" patternUnits="userSpaceOnUse">
    <path d="M17.32 0L34.64 10V30L17.32 40L0 30V10Z M17.32 40V60" fill="none" stroke="{GOLD}" stroke-width="1" stroke-opacity="0.07"/>
  </pattern>
  <pattern id="honeyBright" width="34.64" height="60" patternUnits="userSpaceOnUse">
    <path d="M17.32 0L34.64 10V30L17.32 40L0 30V10Z M17.32 40V60" fill="none" stroke="{HONEY}" stroke-width="1.2" stroke-opacity="0.55"/>
  </pattern>
  <radialGradient id="spot"><stop offset="0" stop-color="#fff" stop-opacity="1"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
  <filter id="glow" x="-80%" y="-80%" width="260%" height="260%">
    <feGaussianBlur stdDeviation="3" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
</defs>
'''


def hive_bg(w, h, sweep_dur=10):
    return f'''<rect width="{w}" height="{h}" rx="18" fill="{BG}"/>
<rect width="{w}" height="{h}" rx="18" fill="url(#honey)"/>
<mask id="sweep"><rect width="{w}" height="{h}" fill="#000"/>
  <circle cy="{h*0.45:.0f}" r="{max(160, h*0.6):.0f}" fill="url(#spot)">
    <animate attributeName="cx" values="-300;{w+300}" dur="{sweep_dur}s" repeatCount="indefinite"/>
  </circle>
</mask>
<rect width="{w}" height="{h}" rx="18" fill="url(#honeyBright)" mask="url(#sweep)"/>
<rect x="0.5" y="0.5" width="{w-1}" height="{h-1}" rx="18" fill="none" stroke="{LINE}"/>
'''


def title_block(x, y, kicker, title, sub=None, right=None, w=1200):
    s = f'<polygon points="{hexpts(x+11, y-8, 12)}" fill="{GOLD}" filter="url(#glow)"/>\n'
    s += f'<text x="{x+34}" y="{y}" font-family="{SANS}" font-size="24" font-weight="800" fill="{TEXT}">{title}</text>\n'
    s += f'<text x="{x+34}" y="{y-26}" font-family="{FONT}" font-size="11" letter-spacing="2" fill="{GOLD}">{esc(kicker)}</text>\n'
    if sub:
        s += f'<text x="{x}" y="{y+30}" font-family="{FONT}" font-size="14" fill="{MUTED}">{esc(sub)}</text>\n'
    if right:
        s += f'<text x="{w-44}" y="{y}" text-anchor="end" font-family="{FONT}" font-size="12" fill="{MUTED}">{esc(right)}</text>\n'
    return s


def car(path_id, dur, begin, color, r=4):
    return (f'<g filter="url(#glow)"><circle r="{r}" fill="{color}"/><circle r="{r*2.2}" fill="{color}" opacity="0.18"/>'
            f'<animateMotion dur="{dur:.2f}s" begin="{begin:.2f}s" repeatCount="indefinite" rotate="auto">'
            f'<mpath xlink:href="#{path_id}"/></animateMotion></g>\n')


def poly_len(pts):
    return sum(math.dist(pts[i], pts[i+1]) for i in range(len(pts)-1))


def write(name, s):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(s + "</svg>\n")
    print(f"  {name:<18} {len(s)/1024:6.1f} KB")


# ------------------------------------------------------------------ network geometry
ROADS = {r["id"]: r for r in NET["roads"]}


def road_pts(r):
    p = r["p"]
    return [(p[i], p[i+1]) for i in range(0, len(p), 2)]


class Frame:
    """Maps local metres (y up) into an SVG rectangle, preserving aspect."""

    def __init__(self, bbox, x, y, w, h, pad=0.06):
        x0, y0, x1, y1 = bbox
        dx, dy = (x1-x0), (y1-y0)
        x0 -= dx*pad; x1 += dx*pad; y0 -= dy*pad; y1 += dy*pad
        # grow the short side so the box matches the target aspect (streets fill the panel)
        cx, cy, bw, bh = (x0+x1)/2, (y0+y1)/2, x1-x0, y1-y0
        if bw/bh < w/h:
            bw = bh*w/h
        else:
            bh = bw*h/w
        x0, x1, y0, y1 = cx-bw/2, cx+bw/2, cy-bh/2, cy+bh/2
        self.s = min(w/(x1-x0), h/(y1-y0))
        self.ox = x + (w - (x1-x0)*self.s)/2 - x0*self.s
        self.oy = y + (h - (y1-y0)*self.s)/2 + y1*self.s
        self.bbox = (x0, y0, x1, y1)

    def __call__(self, px, py):
        return self.ox + px*self.s, self.oy - py*self.s

    def path(self, pts):
        q = [self(*p) for p in pts]
        return "M" + " L".join(f"{a:.0f} {b:.0f}" for a, b in q)

    def inside(self, pts):
        x0, y0, x1, y1 = self.bbox
        return any(x0 <= a <= x1 and y0 <= b <= y1 for a, b in pts)


def base_roads(fr, opacity=1.0, min_lanes=1, dx=0):
    """All streets inside the frame, merged into one path per lane count (small files)."""
    by = {}
    for r in NET["roads"]:
        pts = road_pts(r)
        if r["n"] < min_lanes or not fr.inside(pts):
            continue
        by.setdefault(min(r["n"], 5), []).append(fr.path(pts))
    return "".join(
        f'<path d="{" ".join(ds)}" fill="none" stroke="#2A3140" stroke-opacity="{opacity}" '
        f'stroke-width="{0.6 + 0.45*n:.1f}" stroke-linecap="round" stroke-linejoin="round"/>\n'
        for n, ds in sorted(by.items()))


def corridor_pts(c):
    pts = []
    for e in c["edges"]:
        rp = road_pts(ROADS[e])
        if pts and math.dist(pts[-1], rp[0]) < 0.5:
            rp = rp[1:]
        pts += rp
    return pts


# ------------------------------------------------------------------ 1. HEADER
def header():
    W, H = 1200, 380
    s = svg_open(W, H, "Traffic Hive: what if every car cooperated instead of competing?") + common_defs()
    s += hive_bg(W, H)
    # real network on the right, signals blinking
    fr = Frame((-1430, -1385, 1430, 1385), 700, 18, 480, 344, pad=0.0)
    by = {}
    for r in NET["roads"]:
        by.setdefault(min(r["n"], 5), []).append(fr.path(road_pts(r)))
    s += '<g opacity="0.85">' + "".join(
        f'<path d="{" ".join(ds)}" fill="none" stroke="#2E3646" stroke-width="{0.35 + 0.3*n:.2f}"/>'
        for n, ds in sorted(by.items())) + "</g>\n"
    rnd = random.Random(7)
    for i, L in enumerate(NET["lights"]):
        x, y = fr(L["x"], L["y"])
        d = rnd.uniform(3, 6); b = rnd.uniform(0, d)
        s += (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{GREEN}" filter="url(#glow)">'
              f'<animate attributeName="fill" values="{GREEN};{GREEN};{GOLD};{RED};{RED}" keyTimes="0;0.45;0.55;0.6;1" '
              f'dur="{d:.1f}s" begin="-{b:.1f}s" repeatCount="indefinite"/></circle>\n')
    # cars on the three corridors of the first OD group
    g = CORR["groups"][0]
    s += "<defs>"
    for k, c in enumerate(g["corridors"]):
        s += f'<path id="hc{k}" d="{fr.path(corridor_pts(c))}"/>'
    s += "</defs>\n"
    for k, col in enumerate([GOLD, HONEY, GOLD]):
        s += f'<use xlink:href="#hc{k}" fill="none" stroke="{GOLD}" stroke-opacity="0.35" stroke-width="2.4"/>\n'
        for j in range(4):
            s += car(f"hc{k}", 9 + k, -j*(9 + k)/4 - k, col, r=2.6)
    # fade the map into the card
    s += (f'<defs><linearGradient id="fade" x1="0" x2="1"><stop offset="0" stop-color="{BG}" stop-opacity="1"/>'
          f'<stop offset="0.28" stop-color="{BG}" stop-opacity="0"/></linearGradient></defs>'
          f'<rect x="690" y="1" width="200" height="{H-2}" fill="url(#fade)"/>\n')
    # text
    s += f'<g font-family="{FONT}">\n'
    s += f'<text x="56" y="70" font-size="12" letter-spacing="3" fill="{GOLD}">BSCS THESIS · PAMANTASAN NG LUNGSOD NG MAYNILA</text>\n'
    s += f'<polygon points="{hexpts(82, 128, 26)}" fill="{GOLD}" filter="url(#glow)"/>\n'
    s += f'<polygon points="{hexpts(82, 128, 11)}" fill="{BG}"><animate attributeName="opacity" values="1;0.3;1" dur="2.4s" repeatCount="indefinite"/></polygon>\n'
    s += f'<text x="124" y="146" font-family="{SANS}" font-size="54" font-weight="800" fill="{TEXT}">TRAFFIC <tspan fill="{GOLD}">HIVE</tspan></text>\n'
    lines = ["what if every car cooperated", "instead of competing?"]
    for i, ln in enumerate(lines):
        s += f'<text x="58" y="{204+i*30}" font-size="21" fill="{TEXT}">{ln}</text>\n'
    s += (f'<text x="58" y="286" font-size="14" fill="{MUTED}">Cooperative routing for Manila corridors · '
          f'SUMO · TraCI · HPDM</text>\n')
    chips = ["−11% time loss", "−15% peak queue", "fairer road use"]
    x = 58
    for c in chips:
        w = len(c)*8.2 + 26
        s += (f'<rect x="{x}" y="310" width="{w:.0f}" height="30" rx="15" fill="{PANEL}" stroke="{GOLD}" stroke-opacity="0.6"/>'
              f'<text x="{x+w/2:.0f}" y="330" text-anchor="middle" font-size="13" fill="{GOLD}">{c}</text>\n')
        x += w + 12
    s += f'<text x="{W-24}" y="{H-16}" text-anchor="end" font-size="10" fill="{MUTED}">real España network · 41 live signals</text>\n'
    s += "</g>\n"
    return s


# ------------------------------------------------------------------ 2. SAME DEMAND, TWO RULES
def two_rules():
    W, H = 1200, 640
    g = CORR["groups"][SHOWCASE]
    cps = [corridor_pts(c) for c in g["corridors"]]
    xs = [p[0] for c in cps for p in c]; ys = [p[1] for c in cps for p in c]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    s = svg_open(W, H, "Same demand, two rules: selfish routing piles onto one corridor, Traffic Hive spreads drivers across three") + common_defs()
    s += hive_bg(W, H, 12)
    s += title_block(44, 66, "SIMULATION · REAL ESPAÑA CORRIDORS", f'SAME DEMAND, <tspan fill="{GOLD}">TWO RULES</tspan>',
                     "One origin, one destination, three real corridors. Only the routing rule changes.",
                     right=f"corridors {g['corridors'][0]['length_m']/1000:.1f} · {g['corridors'][1]['length_m']/1000:.1f} · {g['corridors'][2]['length_m']/1000:.1f} km")

    panels = [
        dict(x=40, label="USER EQUILIBRIUM", sub="every driver takes what looks fastest", col=RED,
             mix=[9, 2, 1], speed=[2.1, 1.0, 1.0], key="ue"),
        dict(x=620, label="TRAFFIC HIVE", sub="the hive prices congestion and shares it", col=GOLD,
             mix=[5, 4, 3], speed=[1.0, 1.0, 1.0], key="hive"),
    ]
    PW, PH, PY = 540, 400, 124
    vals = {r[0]: r for r in RESULTS}
    for pi, P in enumerate(panels):
        x = P["x"]
        s += f'<rect x="{x}" y="{PY}" width="{PW}" height="{PH}" rx="14" fill="{PANEL}" fill-opacity="0.9" stroke="{LINE}"/>\n'
        s += f'<clipPath id="clip{pi}"><rect x="{x+1}" y="{PY+1}" width="{PW-2}" height="{PH-2}" rx="13"/></clipPath>\n'
        fr = Frame(bbox, x+20, PY+56, PW-40, PH-76, pad=0.10)
        if pi == 0:
            s += f'<g clip-path="url(#clip{pi})"><g id="streets">' + base_roads(fr) + "</g></g>\n"
        else:
            s += f'<g clip-path="url(#clip{pi})"><use xlink:href="#streets" x="{x - panels[0]["x"]}"/></g>\n'
        s += f'<text x="{x+22}" y="{PY+30}" font-family="{FONT}" font-size="14" font-weight="700" fill="{P["col"]}">{P["label"]}</text>\n'
        s += f'<text x="{x+22}" y="{PY+48}" font-family="{FONT}" font-size="12" fill="{MUTED}">{P["sub"]}</text>\n'
        s += "<defs>"
        for k, c in enumerate(cps):
            s += f'<path id="p{pi}c{k}" d="{fr.path(c)}"/>'
        s += "</defs>\n"
        tot = sum(P["mix"])
        for k, c in enumerate(cps):
            share = P["mix"][k]/tot
            heat = P["col"] if P["key"] == "hive" else (RED if share > 0.5 else GOLD)
            s += (f'<use xlink:href="#p{pi}c{k}" fill="none" stroke="{heat}" stroke-opacity="{0.18+0.55*share:.2f}" '
                  f'stroke-width="{3+10*share:.1f}" stroke-linecap="round" stroke-linejoin="round"/>\n')
            base = 6.0 + 0.0012*poly_len(c)*2
            dur = base*P["speed"][k]
            n = P["mix"][k]
            for j in range(n):
                if P["key"] == "ue" and k == 0:
                    off = -(j*dur/n)*0.55 - (j % 3)*0.18      # bunched platoons
                else:
                    off = -j*dur/max(n, 1)
                col = (HONEY if j % 2 else GOLD) if P["key"] == "hive" else (RED if k == 0 else HONEY)
                s += car(f"p{pi}c{k}", dur, off, col, r=3.4)
        # origin / destination
        o, d = fr(*cps[0][0]), fr(*cps[0][-1])
        for (px, py), t in ((o, "A"), (d, "B")):
            s += (f'<polygon points="{hexpts(px, py, 10)}" fill="{BG}" stroke="{TEXT}" stroke-width="1.6"/>'
                  f'<text x="{px:.1f}" y="{py+4:.1f}" text-anchor="middle" font-family="{FONT}" font-size="11" font-weight="700" fill="{TEXT}">{t}</text>\n')
        # split legend
        lx = x + PW - 150
        for k in range(3):
            share = P["mix"][k]/tot
            yy = PY + 22 + k*15
            s += (f'<text x="{lx}" y="{yy+4}" font-family="{FONT}" font-size="10" fill="{MUTED}">C{k+1}</text>'
                  f'<rect x="{lx+22}" y="{yy-3}" width="100" height="7" rx="3.5" fill="{ROAD}"/>'
                  f'<rect x="{lx+22}" y="{yy-3}" width="{100*share:.0f}" height="7" rx="3.5" fill="{P["col"]}"/>\n')
    # measured strip
    sy = PY + PH + 26
    s += f'<text x="44" y="{sy}" font-family="{FONT}" font-size="11" letter-spacing="2" fill="{GOLD}">MEASURED IN SUMO · 1,000 VEHICLES · 5 SEEDS</text>\n'
    stats = [vals["Time loss per trip"], vals["Waiting per trip"], vals["Peak queue"], vals["Corridor split entropy"]]
    cw = 1112/4
    for i, (lab, ue, hv, unit, low, given) in enumerate(stats):
        cx = 44 + i*cw
        pct = given if given is not None else (hv-ue)/ue*100
        good = (pct < 0) == low
        dec = max(len(f"{u:g}".partition(".")[2]) for u in (ue, hv))
        fmt = lambda v, d=dec: f"{v:.{d}f}"
        s += (f'<text x="{cx}" y="{sy+30}" font-family="{FONT}" font-size="12" fill="{MUTED}">{lab}</text>'
              f'<text x="{cx}" y="{sy+60}" font-family="{SANS}" font-size="24" font-weight="800" fill="{TEXT}">'
              f'<tspan fill="{RED}" font-size="17">{fmt(ue)}</tspan><tspan fill="{MUTED}" font-size="17"> → </tspan>{fmt(hv)}'
              f'<tspan font-size="13" fill="{MUTED}"> {unit}</tspan></text>'
              f'<text x="{cx+228}" y="{sy+60}" text-anchor="end" font-family="{FONT}" font-size="14" font-weight="700" fill="{GREEN if good else RED}">{pct:+.0f}%</text>\n')
    s += f'<text x="{W-24}" y="{H-14}" text-anchor="end" font-family="{FONT}" font-size="10" fill="{MUTED}">animation is illustrative · geometry from corridors.json · numbers from pipeline.evaluate</text>\n'
    return s


# ------------------------------------------------------------------ 3. HOW HPDM DECIDES
def how_it_works():
    W, H = 1200, 430
    s = svg_open(W, H, "How the Hive Path Distribution Mechanism decides: measure, price, filter, choose") + common_defs()
    s += hive_bg(W, H, 11)
    s += title_block(44, 66, "HIVE PATH DISTRIBUTION MECHANISM", f'HOW THE <tspan fill="{GOLD}">HIVE</tspan> DECIDES',
                     "Every 10 seconds, for every departing driver, on the corridors precomputed for their trip.",
                     right="pipeline/run_scenarios.py")
    steps = [
        ("MEASURE", "observed travel time", "per corridor from SUMO,", "smoothed 0.4 on newest"),
        ("PRICE", "marginal cost", "MC = T0 + (n+1)(T − T0)", "what you cost everyone else"),
        ("FILTER", "fairness bound ε = 0.25", "drop corridors slower than", "1.25 × the best for you"),
        ("CHOOSE", "softmax(−η · MC)", "η = 0.03 keeps real mass", "on the alternatives"),
    ]
    cx0, gap, cy = 170, 286, 240
    s += f'<path id="loop" d="M {cx0} {cy} H {cx0+gap*3}" fill="none"/>\n'
    s += f'<line x1="{cx0}" y1="{cy}" x2="{cx0+gap*3}" y2="{cy}" stroke="{LINE}" stroke-width="3"/>\n'
    s += (f'<line x1="{cx0}" y1="{cy}" x2="{cx0+gap*3}" y2="{cy}" stroke="{GOLD}" stroke-opacity="0.6" stroke-width="3" stroke-dasharray="10 12">'
          f'<animate attributeName="stroke-dashoffset" values="0;-44" dur="1s" repeatCount="indefinite"/></line>\n')
    # feedback arc
    s += (f'<path d="M {cx0+gap*3} {cy+64} C {cx0+gap*3} {cy+130}, {cx0} {cy+130}, {cx0} {cy+64}" fill="none" stroke="{GOLD}" '
          f'stroke-opacity="0.35" stroke-width="2" stroke-dasharray="6 8"><animate attributeName="stroke-dashoffset" values="0;28" dur="1s" repeatCount="indefinite"/></path>\n')
    s += f'<text x="{cx0+gap*1.5}" y="{cy+124}" text-anchor="middle" font-family="{FONT}" font-size="12" fill="{MUTED}">assignments change the roads → the next measurement sees it</text>\n'
    s += car("loop", 4, 0, HONEY, r=5)   # drawn first so it slips behind each hex
    for i, (k, a, b, c) in enumerate(steps):
        x = cx0 + i*gap
        s += f'<polygon points="{hexpts(x, cy, 50)}" fill="{PANEL}" stroke="{GOLD}" stroke-width="2"/>\n'
        s += (f'<polygon points="{hexpts(x, cy, 50)}" fill="none" stroke="{HONEY}" stroke-width="3" opacity="0" filter="url(#glow)">'
              f'<animate attributeName="opacity" values="0;1;0;0" keyTimes="0;0.1;0.25;1" dur="4s" begin="{i}s" repeatCount="indefinite"/></polygon>\n')
        s += f'<text x="{x}" y="{cy+5}" text-anchor="middle" font-family="{FONT}" font-size="14" font-weight="700" fill="{GOLD}">{k}</text>\n'
        s += f'<text x="{x}" y="{cy-92}" text-anchor="middle" font-family="{SANS}" font-size="16" font-weight="700" fill="{TEXT}">{esc(a)}</text>\n'
        s += f'<text x="{x}" y="{cy-72}" text-anchor="middle" font-family="{FONT}" font-size="12" fill="{MUTED}">{esc(b)}</text>\n'
        s += f'<text x="{x}" y="{cy-56}" text-anchor="middle" font-family="{FONT}" font-size="12" fill="{MUTED}">{esc(c)}</text>\n'
    return s


# ------------------------------------------------------------------ 4. THE THEORY TOY
def theory():
    th = MET["theory"]
    names = th["corridors"]["names"]
    ue, so = th["allocations"]["UE"], th["allocations"]["SO"]
    W, H = 1200, 470
    s = svg_open(W, H, f"Price of Anarchy {th['price_of_anarchy']:.3f}: selfish drivers all take EDSA, the system optimum moves two to C-5") + common_defs()
    s += hive_bg(W, H, 10)
    s += title_block(44, 66, "ANALYTICAL MODEL · pipeline/theory.py", f'THE PRICE OF <tspan fill="{GOLD}">ANARCHY</tspan>',
                     f"{th['demand']} drivers, three corridors, every possible split enumerated.",
                     right=f"BPR β = {th['corridors']['beta']}, n = {th['corridors']['n']}")
    cols = [(60, "SELFISH (UE)", ue, RED), (620, "SYSTEM OPTIMUM", so, GOLD)]
    for x, lab, al, col in cols:
        s += f'<rect x="{x}" y="120" width="520" height="270" rx="14" fill="{PANEL}" fill-opacity="0.9" stroke="{LINE}"/>\n'
        s += f'<text x="{x+22}" y="150" font-family="{FONT}" font-size="14" font-weight="700" fill="{col}">{lab}</text>\n'
        s += (f'<text x="{x+498}" y="150" text-anchor="end" font-family="{FONT}" font-size="13" fill="{MUTED}">total '
              f'<tspan fill="{TEXT}" font-weight="700">{al["total_cost"]:.1f}</tspan> min</text>\n')
        for k, nm in enumerate(names):
            y = 190 + k*64
            load = al["load"][k]
            s += f'<text x="{x+22}" y="{y+6}" font-family="{FONT}" font-size="13" fill="{TEXT}">{esc(nm)}</text>\n'
            s += f'<text x="{x+22}" y="{y+24}" font-family="{FONT}" font-size="11" fill="{MUTED}">{al["times"][k]:.1f} min each</text>\n'
            for j in range(10):
                hx = x + 190 + j*30
                filled = j < load
                beg = 0.15*j + 0.5*k
                if filled:
                    s += (f'<polygon points="{hexpts(hx, y+6, 12)}" fill="{col}" opacity="0">'
                          f'<animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;{beg/8:.3f};{(beg+0.3)/8:.3f};0.9;1" dur="8s" repeatCount="indefinite"/></polygon>\n')
                else:
                    s += f'<polygon points="{hexpts(hx, y+6, 12)}" fill="none" stroke="{LINE}"/>\n'
    s += (f'<text x="{W/2}" y="430" text-anchor="middle" font-family="{SANS}" font-size="20" font-weight="800" fill="{TEXT}">'
          f'PoA = {ue["total_cost"]:.1f} / {so["total_cost"]:.1f} = <tspan fill="{GOLD}">{th["price_of_anarchy"]:.3f}</tspan>'
          f'<tspan font-size="14" fill="{MUTED}" font-weight="400">   ·  coordination can save {th["so_improvement_pct"]:.1f}% here</tspan></text>\n')
    s += f'<text x="{W-24}" y="{H-14}" text-anchor="end" font-family="{FONT}" font-size="10" fill="{MUTED}">illustrative free-flow times · see "On the analytical corridor parameters"</text>\n'
    return s


# ------------------------------------------------------------------ 5. RESULTS BARS
def results():
    W, H = 1200, 520
    s = svg_open(W, H, "Measured results, User equilibrium versus Traffic Hive, España, 1000 vehicles, 5 seeds") + common_defs()
    s += hive_bg(W, H, 12)
    s += title_block(44, 66, "MEASURED · python -m pipeline.evaluate", f'UE <tspan fill="{MUTED}">vs</tspan> <tspan fill="{GOLD}">HIVE</tspan>',
                     "España, rebuilt network · 1,000 vehicles · 15-minute demand window · mean of 5 seeds",
                     right="lower is better unless marked ↑")
    s += (f'<rect x="900" y="104" width="12" height="12" rx="3" fill="{RED}" fill-opacity="0.75"/>'
          f'<text x="918" y="115" font-family="{FONT}" font-size="12" fill="{MUTED}">User equilibrium</text>'
          f'<rect x="1040" y="104" width="12" height="12" rx="3" fill="{GOLD}"/>'
          f'<text x="1058" y="115" font-family="{FONT}" font-size="12" fill="{MUTED}">Traffic Hive</text>\n')
    y0, row = 150, 56
    bx, bw = 330, 640
    for i, (lab, ue, hv, unit, low, given) in enumerate(RESULTS):
        y = y0 + i*row
        mx = max(ue, hv)*1.08
        pct = given if given is not None else (hv-ue)/ue*100
        good = (pct < 0) == low
        s += f'<text x="60" y="{y+16}" font-family="{SANS}" font-size="16" font-weight="600" fill="{TEXT}">{lab}{"" if low else " ↑"}</text>\n'
        for j, (v, col, op) in enumerate(((ue, RED, 0.75), (hv, GOLD, 1))):
            w = bw*v/mx
            yy = y + j*18
            d0, d1 = 0.15*i, 0.15*i + 1.1
            s += f'<rect x="{bx}" y="{yy}" width="{bw}" height="13" rx="6.5" fill="{ROAD}"/>\n'
            s += (f'<rect x="{bx}" y="{yy}" height="13" rx="6.5" fill="{col}" fill-opacity="{op}" width="0">'
                  f'<animate attributeName="width" values="0;0;{w:.1f};{w:.1f};0" keyTimes="0;{d0/9:.3f};{d1/9:.3f};0.93;1" dur="9s" repeatCount="indefinite"/></rect>\n')
            dec = max(len(f"{u:g}".partition(".")[2]) for u in (ue, hv))
            txt = f"{v:.{dec}f}{(' ' + unit) if unit and unit != '%' else unit}"
            s += f'<text x="{bx+w+10:.1f}" y="{yy+11}" font-family="{FONT}" font-size="12" fill="{TEXT if j else MUTED}">{txt}</text>\n'
        s += (f'<text x="{W-60}" y="{y+24}" text-anchor="end" font-family="{SANS}" font-size="20" font-weight="800" '
              f'fill="{GREEN if good else RED}">{pct:+.0f}%</text>\n')
    s += (f'<text x="60" y="{H-24}" font-family="{FONT}" font-size="12" fill="{MUTED}">Hive cut time loss in 9 of 10 seeded runs · '
          f'with the 12 España obstacles: 253 s → 237 s, ahead in 5/5 seeds</text>\n')
    return s


# ------------------------------------------------------------------ 6. QUEUE OVER TIME
def queue():
    m = MET["metrics"]
    ue, hv = m["ue_corridor"]["series"], m["hive_corridor"]["series"]
    W, H = 1200, 480
    s = svg_open(W, H, f"Vehicles stopped over time in a stress run: UE peaks at {m['ue_corridor']['peak_halting']}, Hive at {m['hive_corridor']['peak_halting']}") + common_defs()
    s += hive_bg(W, H, 12)
    s += title_block(44, 66, "STRESS RUN · app/assets/sim/metrics.json", f'QUEUES <tspan fill="{GOLD}">OVER TIME</tspan>',
                     "Vehicles stopped across the network, ~3,400 inserted over 25 minutes, one seed.",
                     right="oversaturated on purpose")
    X0, Y0, PW, PH = 110, 130, 1030, 270
    tmax = max(ue["t"][-1], hv["t"][-1]); ymax = 1600
    def P(t, v):
        return X0 + PW*t/tmax, Y0 + PH - PH*v/ymax
    for v in range(0, ymax+1, 400):
        _, y = P(0, v)
        s += (f'<line x1="{X0}" y1="{y:.1f}" x2="{X0+PW}" y2="{y:.1f}" stroke="{LINE}" stroke-dasharray="2 6"/>'
              f'<text x="{X0-12}" y="{y+4:.1f}" text-anchor="end" font-family="{FONT}" font-size="11" fill="{MUTED}">{v}</text>\n')
    for t in range(0, int(tmax)+1, 300):
        x, _ = P(t, 0)
        s += f'<text x="{x:.1f}" y="{Y0+PH+22}" text-anchor="middle" font-family="{FONT}" font-size="11" fill="{MUTED}">{t//60} min</text>\n'
    for ser, col, key, w in ((ue, RED, "ue_corridor", 2.2), (hv, GOLD, "hive_corridor", 3)):
        pts = [P(t, v) for t, v in zip(ser["t"], ser["halting"])]
        d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        L = sum(math.dist(pts[i], pts[i+1]) for i in range(len(pts)-1))
        area = d + f" L{pts[-1][0]:.1f} {Y0+PH} L{pts[0][0]:.1f} {Y0+PH} Z"
        s += f'<path d="{area}" fill="{col}" fill-opacity="0.07"/>\n'
        s += (f'<path d="{d}" fill="none" stroke="{col}" stroke-width="{w}" stroke-linejoin="round" '
              f'stroke-dasharray="{L:.0f}" stroke-dashoffset="{L:.0f}" filter="url(#glow)">'
              f'<animate attributeName="stroke-dashoffset" values="{L:.0f};0;0;{L:.0f}" keyTimes="0;0.55;0.92;1" dur="10s" repeatCount="indefinite"/></path>\n')
        pk = m[key]["peak_halting"]
        i = max(range(len(ser["halting"])), key=lambda j: ser["halting"][j])
        px, py = pts[i]
        s += (f'<g opacity="0"><animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;0.5;0.56;0.92;1" dur="10s" repeatCount="indefinite"/>'
              f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5" fill="{col}"/>'
              f'<text x="{px+10:.1f}" y="{py-8:.1f}" font-family="{FONT}" font-size="13" font-weight="700" fill="{col}">peak {pk:,}</text></g>\n')
    a, b = m["ue_corridor"], m["hive_corridor"]
    s += (f'<text x="{X0}" y="{H-22}" font-family="{FONT}" font-size="12" fill="{MUTED}">'
          f'<tspan fill="{RED}">━ User equilibrium</tspan>   <tspan fill="{GOLD}">━ Traffic Hive</tspan>   ·   '
          f'trips completed {a["completion_rate"]*100:.0f}% → {b["completion_rate"]*100:.0f}%   ·   '
          f'p90 trip {a["duration_p90_s"]/60:.1f} → {b["duration_p90_s"]/60:.1f} min</text>\n')
    return s


# ------------------------------------------------------------------ 7. PIPELINE
def pipeline():
    W, H = 1200, 400
    s = svg_open(W, H, "Pipeline: OpenStreetMap to network, audit, scenarios in SUMO, pack, then playback, server and figures") + common_defs()
    s += hive_bg(W, H, 11)
    s += title_block(44, 66, "HOW IT FITS TOGETHER", f'FROM <tspan fill="{GOLD}">OSM</tspan> TO THE DEFENSE ROOM',
                     "SUMO computes everything ahead of time. The screens only play it back.", right="make all")
    main = [("OSM", "map.osm", "data/raw"), ("NET", "make net", "SUMO network"), ("AUDIT", "make audit", "the gate"),
            ("RUN", "make runs", "4 rules · 1 demand"), ("PACK", "make pack", "THV1 binary + metrics")]
    outs = [("APP", "Expo + Skia playback"), ("HIVE OUT", "driver view + obstacles"), ("SERVER", "FastAPI run-on-demand")]
    y = 200; x0 = 90; gap = 180
    s += f'<path id="pl" d="M {x0} {y} H {x0+gap*4}"/>\n'
    s += f'<line x1="{x0}" y1="{y}" x2="{x0+gap*4}" y2="{y}" stroke="{LINE}" stroke-width="3"/>\n'
    for j in range(3):
        s += car("pl", 4.5, -j*1.5, GOLD, r=4)
    for i, (k, cmd, note) in enumerate(main):
        x = x0 + i*gap
        s += f'<polygon points="{hexpts(x, y, 38)}" fill="{PANEL}" stroke="{GOLD}" stroke-width="2"/>\n'
        s += f'<text x="{x}" y="{y+5}" text-anchor="middle" font-family="{FONT}" font-size="13" font-weight="700" fill="{GOLD}">{k}</text>\n'
        s += f'<text x="{x}" y="{y+64}" text-anchor="middle" font-family="{FONT}" font-size="12" fill="{TEXT}">{esc(cmd)}</text>\n'
        s += f'<text x="{x}" y="{y+82}" text-anchor="middle" font-family="{FONT}" font-size="11" fill="{MUTED}">{esc(note)}</text>\n'
    px = x0 + gap*4
    for i, (k, note) in enumerate(outs):
        oy = 130 + i*76
        s += (f'<path d="M {px+38} {y} C {px+90} {y}, {px+90} {oy}, {px+140} {oy}" fill="none" stroke="{GOLD}" stroke-opacity="0.5" '
              f'stroke-width="2" stroke-dasharray="6 6"><animate attributeName="stroke-dashoffset" values="0;-24" dur="1s" repeatCount="indefinite"/></path>\n')
        s += f'<rect x="{px+140}" y="{oy-22}" width="160" height="44" rx="10" fill="{PANEL}" stroke="{LINE}"/>\n'
        s += f'<text x="{px+156}" y="{oy-3}" font-family="{FONT}" font-size="12" font-weight="700" fill="{TEXT}">{k}</text>\n'
        s += f'<text x="{px+156}" y="{oy+13}" font-family="{FONT}" font-size="10" fill="{MUTED}">{esc(note)}</text>\n'
    side = "make calibrate  ·  make theory  ·  make figures  ·  python -m pipeline.evaluate"
    s += f'<text x="{x0-30}" y="{H-30}" font-family="{FONT}" font-size="12" fill="{MUTED}">analysis on the side:  <tspan fill="{HONEY}">{side}</tspan></text>\n'
    return s


# ------------------------------------------------------------------ 8. DIVIDER + FOOTER
def divider():
    W, H = 1200, 44
    s = svg_open(W, H, "road divider") + common_defs()
    s += f'<rect x="0" y="8" width="{W}" height="28" rx="14" fill="{ROAD}"/>\n'
    s += (f'<line x1="14" y1="22" x2="{W-14}" y2="22" stroke="{GOLD}" stroke-opacity="0.45" stroke-width="2" stroke-dasharray="16 14">'
          f'<animate attributeName="stroke-dashoffset" values="0;-60" dur="1s" repeatCount="indefinite"/></line>\n')
    s += f'<defs><path id="dl1" d="M -20 15 H {W+20}"/><path id="dl2" d="M {W+20} 29 H -20"/></defs>\n'
    for b in (0, -3.3, -6.6):
        s += car("dl1", 10, b, GOLD, r=3.4)
    for b in (-1.5, -5.5):
        s += car("dl2", 12, b, HONEY, r=3.4)
    return s


def footer():
    W, H = 1200, 150
    s = svg_open(W, H, "Next stop: a city that moves together") + common_defs()
    s += hive_bg(W, H, 9)
    s += f'<defs><path id="fr" d="M -40 112 C 300 92, 900 132, 1240 108"/></defs>\n'
    s += f'<use xlink:href="#fr" fill="none" stroke="{ROAD}" stroke-width="22" stroke-linecap="round"/>\n'
    for b in range(6):
        s += car("fr", 9, -b*1.5, GOLD if b % 2 else HONEY, r=3.4)
    s += (f'<text x="{W/2}" y="64" text-anchor="middle" font-family="{FONT}" font-size="20" fill="{TEXT}">next stop: '
          f'<tspan fill="{GOLD}">a city that moves together</tspan></text>\n')
    return s


if __name__ == "__main__":
    print("writing to", os.path.relpath(OUT, ROOT))
    for name, fn in [("header.svg", header), ("two-rules.svg", two_rules), ("how-it-works.svg", how_it_works),
                     ("price-of-anarchy.svg", theory), ("results.svg", results), ("queue.svg", queue),
                     ("pipeline.svg", pipeline), ("divider.svg", divider), ("footer.svg", footer)]:
        write(name, fn())
