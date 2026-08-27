#!/usr/bin/env python3
"""Render an isometric "contribution district" SVG from real GitHub data.

Every number and every block in the output comes from the GitHub GraphQL API,
so the graphic can never drift from the profile it describes.

Usage:
    GH_LOGIN=<user> GITHUB_TOKEN=<token> python scripts/build_city.py
Locally the token falls back to `gh auth token`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

LOGIN = os.environ.get("GH_LOGIN", "virtual41tridiv")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "assets", "contrib-city.svg")

# ---------------------------------------------------------------- palette ---
BG = "#0b1017"
CARD_STROKE = "#1c2431"
SLAB = "#111823"
SKIRT = "#0c121a"
PAD = "#222d3d"          # a day with no contributions
TEXT = "#e6edf3"
MUTED = "#7d8590"
ACCENT = "#7c6cf0"
LEVELS = ["#1b5e5a", "#2a9d8f", "#4cc9c0", "#7c6cf0"]  # level 1 -> 4

HW, HH = 10.0, 5.0       # isometric tile pitch (half-width / half-height)
GX, GY = 9.0, 4.5        # drawn tile size, slightly inset to leave street gaps
BASE, STEP = 8.0, 4.0    # tower height for one contribution, then per extra


def shade(hex_color: str, factor: float) -> str:
    """Darken #rrggbb toward black. factor 0 = black, 1 = unchanged."""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return "#%02x%02x%02x" % (int(r * factor), int(g * factor), int(b * factor))


# ------------------------------------------------------------------ query ---
def token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception:
        sys.exit("No GITHUB_TOKEN in the environment and `gh auth token` failed.")


def graphql(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": "bearer " + token(),
            "Content-Type": "application/json",
            "User-Agent": "contribution-city",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"GitHub API error {exc.code}: {exc.read().decode()[:400]}")
    if "errors" in payload:
        sys.exit("GitHub API error: " + json.dumps(payload["errors"])[:400])
    return payload["data"]


QUERY = """
query($login:String!, $from:DateTime!, $to:DateTime!) {
  user(login:$login) {
    createdAt
    followers { totalCount }
    repositories(privacy: PUBLIC, ownerAffiliations: OWNER, isFork: false) {
      totalCount
    }
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount weekday } }
      }
    }
  }
}
"""

LIFETIME = """
query($login:String!) { user(login:$login) { %s } }
"""


def fetch() -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=364)
    data = graphql(QUERY, {
        "login": LOGIN,
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": now.isoformat().replace("+00:00", "Z"),
    })["user"]

    # Lifetime total: one aliased contributionsCollection per year since signup.
    first_year = int(data["createdAt"][:4])
    years = range(first_year, now.year + 1)
    fields = "\n".join(
        f'y{y}: contributionsCollection('
        f'from:"{y}-01-01T00:00:00Z", to:"{y}-12-31T23:59:59Z")'
        f'{{ contributionCalendar {{ totalContributions }} }}'
        for y in years
    )
    life = graphql(LIFETIME % fields, {"login": LOGIN})["user"]
    data["lifetime"] = sum(
        life[f"y{y}"]["contributionCalendar"]["totalContributions"] for y in years
    )
    data["since"] = first_year
    data["window"] = (start, now)
    return data


# ------------------------------------------------------------------ render --
def polygon(points, fill) -> str:
    pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return f'<polygon points="{pts}" fill="{fill}"/>'


def cube(cx: float, cy: float, height: float, color: str) -> str:
    """Draw an extruded isometric block whose top-face centre sits `height`
    above the ground point (cx, cy)."""
    ty = cy - height
    top = [(cx, ty - GY), (cx + GX, ty), (cx, ty + GY), (cx - GX, ty)]
    left = [(cx - GX, ty), (cx, ty + GY), (cx, cy + GY), (cx - GX, cy)]
    right = [(cx + GX, ty), (cx, ty + GY), (cx, cy + GY), (cx + GX, cy)]
    out = ""
    if height > 0.01:
        out += polygon(left, shade(color, 0.55))
        out += polygon(right, shade(color, 0.36))
    out += polygon(top, color)
    return out


def level_of(count: int, peak: int) -> int:
    if count <= 0:
        return 0
    for i, cut in enumerate((0.25, 0.5, 0.75), start=1):
        if count <= max(1, round(peak * cut)):
            return i
    return 4


def streaks(days) -> tuple[int, int]:
    longest = run = 0
    for d in days:
        run = run + 1 if d["contributionCount"] > 0 else 0
        longest = max(longest, run)
    current = 0
    for d in reversed(days):
        if d["contributionCount"] > 0:
            current += 1
        else:
            break
    return current, longest


def build(data: dict) -> str:
    cal = data["contributionsCollection"]["contributionCalendar"]
    weeks = cal["weeks"]
    days = [d for w in weeks for d in w["contributionDays"]]
    peak = max((d["contributionCount"] for d in days), default=0)
    active = sum(1 for d in days if d["contributionCount"] > 0)
    _, longest = streaks(days)

    # --- lay the grid out in isometric space, origin at (0, 0) -------------
    cells = []  # (depth, cx, cy, height, colour)
    for col, week in enumerate(weeks):
        for day in week["contributionDays"]:
            row = day["weekday"]
            count = day["contributionCount"]
            cx = (col - row) * HW
            cy = (col + row) * HH
            lvl = level_of(count, peak)
            if lvl == 0:
                cells.append((col + row, cx, cy, 2.0, PAD))
            else:
                cells.append((col + row, cx, cy,
                              BASE + (count - 1) * STEP, LEVELS[lvl - 1]))
    cells.sort(key=lambda c: (c[0], c[1]))

    max_col = len(weeks) - 1
    # outer corners of the ground plate
    north = (0.0, -HH)
    east = (max_col * HW + HW, max_col * HH)
    south = ((max_col - 6) * HW, (max_col + 6) * HH + HH)
    west = (-6 * HW - HW, 6 * HH)

    # Real bounding box: the tallest tower is not necessarily the highest
    # point on screen, so measure every block rather than assuming.
    min_x, max_x = west[0], east[0]
    min_y = min([north[1]] + [cy - h - GY for _, _, cy, h, _ in cells])
    max_y = south[1] + 7  # + slab skirt

    header_h = 92
    footer_h = 62
    pad_x, pad_top, pad_bottom = 30, 14, 22

    off_x = pad_x - min_x
    off_y = header_h + pad_top - min_y

    width = round(max_x - min_x + pad_x * 2)
    height = round(max_y - min_y + header_h + pad_top + footer_h + pad_bottom)

    def T(p):
        return (p[0] + off_x, p[1] + off_y)

    s = []
    s.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Isometric map of GitHub contributions">'
    )
    s.append(
        '<style>'
        '.t{font-family:ui-monospace,SFMono-Regular,SF Mono,Menlo,Consolas,monospace}'
        '.s{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif}'
        '</style>'
    )
    s.append(f'<rect width="{width}" height="{height}" rx="14" fill="{BG}" '
             f'stroke="{CARD_STROKE}"/>')

    # --- header ------------------------------------------------------------
    start, end = data["window"]
    span = f'{start:%b %Y} → {end:%b %Y}'
    s.append(f'<text class="t" x="30" y="40" fill="{TEXT}" font-size="15" '
             f'font-weight="600" letter-spacing="2.4">CONTRIBUTION DISTRICT</text>')
    s.append(f'<text class="s" x="30" y="62" fill="{MUTED}" font-size="12">'
             f'{cal["totalContributions"]} contributions &#183; {span}</text>')
    s.append(f'<rect x="30" y="76" width="34" height="2" rx="1" fill="{ACCENT}"/>')

    # legend, right-aligned in the header: less -> more, left to right
    right = width - 30
    swatch, gap = 11, 4
    strip = len(LEVELS) * swatch + (len(LEVELS) - 1) * gap
    bx = right - 34 - strip                      # 34 = room for the "more" label
    s.append(f'<text class="s" x="{bx - 8:.1f}" y="40" fill="{MUTED}" '
             f'font-size="11" text-anchor="end">less</text>')
    for i, color in enumerate(LEVELS):
        s.append(f'<rect x="{bx + i * (swatch + gap):.1f}" y="30" '
                 f'width="{swatch}" height="{swatch}" rx="2" fill="{color}"/>')
    s.append(f'<text class="s" x="{bx + strip + 8:.1f}" y="40" fill="{MUTED}" '
             f'font-size="11">more</text>')

    # --- ground plate ------------------------------------------------------
    n, e, sth, w = T(north), T(east), T(south), T(west)
    d = 7
    s.append(
        f'<path d="M{w[0]:.2f},{w[1]:.2f} L{sth[0]:.2f},{sth[1]:.2f} '
        f'L{e[0]:.2f},{e[1]:.2f} L{e[0]:.2f},{e[1] + d:.2f} '
        f'L{sth[0]:.2f},{sth[1] + d:.2f} L{w[0]:.2f},{w[1] + d:.2f} Z" '
        f'fill="{SKIRT}"/>'
    )
    s.append(polygon([n, e, sth, w], SLAB))

    # --- blocks, painted back to front ------------------------------------
    for _, cx, cy, h, color in cells:
        s.append(cube(cx + off_x, cy + off_y, h, color))

    # --- footer stats ------------------------------------------------------
    fy = height - footer_h + 6
    s.append(f'<rect x="30" y="{fy - 14}" width="{width - 60}" height="1" '
             f'fill="{CARD_STROKE}"/>')
    stats = [
        (f'{cal["totalContributions"]}', "past year"),
        (f'{active}', "active days"),
        (f'{longest}', "longest streak"),
        (f'{data["repositories"]["totalCount"]}', "public repos"),
        (f'{data["lifetime"]}', f'since {data["since"]}'),
    ]
    slot = (width - 60) / len(stats)
    for i, (value, label) in enumerate(stats):
        x = 30 + slot * i + slot / 2
        s.append(f'<text class="t" x="{x:.1f}" y="{fy + 14}" fill="{TEXT}" '
                 f'font-size="17" font-weight="600" text-anchor="middle">{value}</text>')
        s.append(f'<text class="s" x="{x:.1f}" y="{fy + 32}" fill="{MUTED}" '
                 f'font-size="11" text-anchor="middle">{label}</text>')

    s.append("</svg>")
    return "".join(s)


def main() -> None:
    svg = build(fetch())
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"wrote {OUT} ({len(svg)} bytes)")


if __name__ == "__main__":
    main()
