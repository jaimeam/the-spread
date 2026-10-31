"""Listener stats from OP3 (op3.dev) -> site/stats.json, site/stats.html, site/stats.svg.

    python stats.py           # fetch from OP3 (needs OP3_TOKEN), then render
    python stats.py render    # re-render the page and card from site/stats.json, offline

OP3 counts downloads of episodes whose MP3 link goes through op3.dev (from `op3_from` in
podcast.toml on), excludes bots, and updates once a day. Only aggregates are saved here,
never per-download rows. Standard library only.
"""

import html
import json
import os
import sys
import tomllib
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
OUT_JSON = SITE / "stats.json"
API = "https://op3.dev/api/1"
DAYS = 60       # daily chart window
TOP = 8         # apps / countries listed


def cfg() -> dict:
    with open(ROOT / "podcast.toml", "rb") as f:
        return tomllib.load(f)["podcast"]


# --------------------------------------------------------------------------- fetch

def get(path: str, **params) -> dict:
    token = os.environ.get("OP3_TOKEN")
    if not token:
        sys.exit("OP3_TOKEN is not set (an OP3 bearer token from https://op3.dev/api/keys)")
    url = f"{API}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "User-Agent": "the-spread-stats"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def downloads(show_uuid: str, start: str) -> list[dict]:
    """Every non-bot download since `start`, following OP3's continuation tokens."""
    rows, token = [], None
    while True:
        params = {"format": "json", "start": start, "limit": 20000}
        if token:
            params["continuationToken"] = token
        page = get(f"/downloads/show/{show_uuid}", **params)
        rows += page.get("rows", [])
        token = page.get("continuationToken")
        if not token:
            return rows


def collect(fetch=get, fetch_downloads=downloads, today: date | None = None) -> dict:
    c = cfg()
    today = today or datetime.now(timezone.utc).date()
    show = fetch(f"/shows/{c['guid']}")
    uuid = show["showUuid"]
    counts = fetch("/queries/show-download-counts", showUuid=uuid)
    show_counts = counts["showDownloadCounts"].get(uuid, {})
    eps = fetch("/queries/episode-download-counts", showUuid=uuid)

    start = max(c.get("op3_from", ""), (today - timedelta(days=DAYS)).isoformat())
    rows = fetch_downloads(uuid, start)
    daily, apps, countries = Counter(), Counter(), Counter()
    audience_30 = set()
    cutoff_30 = (today - timedelta(days=30)).isoformat()
    for r in rows:
        day = r["time"][:10]
        daily[day] += 1
        apps[r.get("agentName") or "Unknown"] += 1
        countries[r.get("countryCode") or "??"] += 1
        if day >= cutoff_30 and r.get("audienceId"):
            audience_30.add(r["audienceId"])

    prefix = c["slug"] + "-"
    episodes = [{
        "date": e["itemGuid"].removeprefix(prefix) if e.get("itemGuid", "").startswith(prefix) else e.get("pubdate", "")[:10],
        "title": e.get("title", ""),
        **{k: e.get(k) for k in ("downloads1", "downloads3", "downloads7", "downloads30", "downloadsAll")},
    } for e in eps.get("episodes", [])]

    first = date.fromisoformat(start)
    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "asof": counts.get("asof"),
        "show_uuid": uuid,
        "op3_page": show.get("statsPageUrl") or f"https://op3.dev/show/{uuid}",
        "op3_from": c.get("op3_from"),
        "monthly_downloads": show_counts.get("monthlyDownloads", 0),
        "weekly_avg_downloads": show_counts.get("weeklyAvgDownloads", 0),
        "weekly_downloads": show_counts.get("weeklyDownloads", []),
        "audience_30d": len(audience_30),
        "all_time": sum(e["downloadsAll"] or 0 for e in episodes),
        "daily": [{"date": d.isoformat(), "downloads": daily.get(d.isoformat(), 0)}
                  for d in (first + timedelta(n) for n in range((today - first).days))],
        "apps": apps.most_common(TOP),
        "countries": countries.most_common(TOP),
        "episodes": episodes,
    }


# --------------------------------------------------------------------------- render

# Single-series palette (blue), recessive ink and grid; dark mode has its own steps.
CSS = """
:root { color-scheme: light; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --muted:#8a8984;
  --grid:#e4e3df; --bar:#2a78d6; --bar-hover:#1c5cab; --track:#efeeea; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --surface:#1a1a19; --ink:#f0efec;
  --ink-2:#c3c2b7; --muted:#8f8e86; --grid:#33322f; --bar:#3987e5; --bar-hover:#6da7ec; --track:#2a2927; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--surface); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:960px; margin:0 auto; padding:32px 20px 64px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:17px; margin:40px 0 12px; }
.sub, .note { color:var(--ink-2); } .note { font-size:13px; }
a { color:inherit; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:16px; margin-top:24px; }
.tile { border:1px solid var(--grid); border-radius:8px; padding:14px 16px; }
.tile .v { font-size:30px; font-weight:600; font-variant-numeric:tabular-nums; }
.tile .k { color:var(--ink-2); font-size:13px; }
svg text { fill:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
svg .grid { stroke:var(--grid); stroke-width:1; }
svg .bar { fill:var(--bar); } svg g.col:hover .bar { fill:var(--bar-hover); }
svg .hit { fill:transparent; }
svg .tip { opacity:0; pointer-events:none; } svg g.col:hover .tip { opacity:1; }
svg .tip text { fill:var(--ink); font-size:12px; }
svg .tip rect { fill:var(--surface); stroke:var(--grid); }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { text-align:right; padding:6px 8px; border-bottom:1px solid var(--grid); white-space:nowrap; }
th { color:var(--ink-2); font-weight:500; font-size:13px; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align:left; }
td:nth-child(2) { white-space:normal; }
.two { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:32px; }
.row { display:grid; grid-template-columns:1fr 3fr auto; gap:10px; align-items:center; margin:6px 0; }
.row .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.row .track { background:var(--track); border-radius:4px; height:10px; }
.row .fill { background:var(--bar); height:10px; border-radius:0 4px 4px 0; }
.row .n { color:var(--ink-2); font-variant-numeric:tabular-nums; min-width:3ch; text-align:right; }
"""


def fmt(n) -> str:
    return "–" if n is None else f"{n:,.0f}"


def nice_max(v: int) -> int:
    """Top of the y axis: four gridline steps of 1, 2, 5, 10, 20, 50... so every tick is whole."""
    step = 1
    while step * 4 < v:
        step = step * 5 // 2 if str(step)[0] == "2" else step * 2
    return step * 4


def daily_svg(daily: list[dict], width=920, height=220, tips=True) -> str:
    """Column chart of downloads per day. One series: no legend, the heading names it."""
    left, right, top, bottom = 34, 8, 10, 24
    w, h = width - left - right, height - top - bottom
    ymax = nice_max(max([d["downloads"] for d in daily] + [1]))
    n = max(len(daily), 1)
    slot = w / n
    bw = min(24, max(2, slot - 2))  # 2px gap between adjacent columns, 24px cap
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
           f'aria-label="Downloads per day, last {n} days">']
    for i in range(5):
        v = ymax * i / 4
        y = top + h - h * i / 4
        out.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{v:,.0f}</text>')
    step = max(1, n // 6)
    for i, d in enumerate(daily):
        x = left + i * slot + (slot - bw) / 2
        bh = h * d["downloads"] / ymax
        y = top + h - bh
        r = min(4, bw / 2, bh)
        # Rounded at the data end, square at the baseline.
        path = (f"M{x:.1f},{top + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
                f"H{x + bw - r:.1f}Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f}V{top + h:.1f}Z") if bh else ""
        label = f'{date.fromisoformat(d["date"]):%a %d %b}: {d["downloads"]:,} downloads'
        out.append(f'<g class="col"><title>{label}</title>'
                   f'<rect class="hit" x="{left + i * slot:.1f}" y="{top}" width="{slot:.1f}" height="{h}"/>'
                   + (f'<path class="bar" d="{path}"/>' if path else ""))
        if tips:
            tx = min(max(x + bw / 2, left + 70), width - right - 70)
            out.append(f'<g class="tip"><rect x="{tx - 68:.1f}" y="{top}" width="136" height="22" rx="4"/>'
                       f'<text x="{tx:.1f}" y="{top + 15}" text-anchor="middle">{html.escape(label.replace(" downloads", ""))}</text></g>')
        out.append("</g>")
        if i % step == 0 or i == n - 1:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{height - 6}" text-anchor="middle">'
                       f'{date.fromisoformat(d["date"]):%d %b}</text>')
    out.append("</svg>")
    return "".join(out)


def bar_list(pairs: list, names: dict | None = None) -> str:
    if not pairs:
        return '<p class="note">No data yet.</p>'
    top = max(n for _, n in pairs)
    return "".join(
        f'<div class="row"><span class="name">{html.escape((names or {}).get(k, k))}</span>'
        f'<span class="track"><span class="fill" style="display:block;width:{100 * n / top:.1f}%"></span></span>'
        f'<span class="n">{n:,}</span></div>' for k, n in pairs)


def page(s: dict, c: dict) -> str:
    title = html.escape(c["title"])
    total_window = sum(d["downloads"] for d in s["daily"])
    eps = "".join(
        f'<tr><td>{html.escape(e["date"])}</td><td>{html.escape(e["title"])}</td>'
        f'<td>{fmt(e["downloads1"])}</td><td>{fmt(e["downloads7"])}</td>'
        f'<td>{fmt(e["downloads30"])}</td><td>{fmt(e["downloadsAll"])}</td></tr>'
        for e in s["episodes"])
    tiles = [("Downloads, last 30 days", s["monthly_downloads"]),
             ("Average per week", s["weekly_avg_downloads"]),
             ("Listeners, last 30 days (est.)", s["audience_30d"]),
             ("All-time downloads", s["all_time"])]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Listener stats · {title}</title>
<style>{CSS}</style></head>
<body><main>
<h1>Listener stats</h1>
<p class="sub">{title}. Downloads counted by <a href="{html.escape(s["op3_page"])}">OP3</a>, the open podcast
analytics service, bots excluded. Data to {html.escape(s.get("asof") or "?")}, page updated {html.escape(s["updated"])}.</p>
<div class="tiles">{"".join(f'<div class="tile"><div class="v">{fmt(v)}</div><div class="k">{k}</div></div>' for k, v in tiles)}</div>

<h2>Downloads per day</h2>
{daily_svg(s["daily"])}
<p class="note">{fmt(total_window)} downloads over the {len(s["daily"])} days shown. Hover a column for the day's count.</p>

<h2>Episodes</h2>
<div style="overflow-x:auto"><table><thead><tr><th>Date</th><th>Episode</th><th>First day</th><th>First 7 days</th><th>First 30 days</th><th>All time</th></tr></thead>
<tbody>{eps or '<tr><td colspan="6">No episodes counted yet.</td></tr>'}</tbody></table></div>

<div class="two">
<section><h2>Apps</h2>{bar_list(s["apps"])}</section>
<section><h2>Countries</h2>{bar_list(s["countries"])}</section>
</div>

<p class="note" style="margin-top:40px">Only episodes from {html.escape(s.get("op3_from") or "?")} on are counted: older
episodes keep their original MP3 link. Listeners are estimated from OP3's anonymous audience IDs.
Apps and countries cover the {len(s["daily"])} days shown. Raw data: <a href="stats.json">stats.json</a> ·
<a href="feed.xml">podcast feed</a>.</p>
</main></body></html>
"""


def card(s: dict) -> str:
    """Preview image for the README: headline numbers + the last 30 days. Light colors only
    (GitHub shows README images on both themes, so the card brings its own background)."""
    daily = s["daily"][-30:]
    chart = daily_svg(daily, width=560, height=130, tips=False)
    chart = chart.replace("<svg ", '<svg x="20" y="88" width="560" height="130" ', 1).replace(' width="100%"', "")
    nums = [(s["monthly_downloads"], "downloads, 30 days"), (s["weekly_avg_downloads"], "avg per week"),
            (s["audience_30d"], "listeners (est.)"), (s["all_time"], "all time")]
    tiles = "".join(f'<text x="{24 + i * 142}" y="62" class="v">{fmt(v)}</text>'
                    f'<text x="{24 + i * 142}" y="80" class="k">{k}</text>' for i, (v, k) in enumerate(nums))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 236" width="600" height="236" role="img" aria-label="Podcast downloads: {fmt(s["monthly_downloads"])} in the last 30 days">
<style>text {{ font-family:system-ui,-apple-system,'Segoe UI',sans-serif; fill:#8a8984; font-size:11px; }}
.h {{ fill:#0b0b0b; font-size:15px; font-weight:600; }} .v {{ fill:#0b0b0b; font-size:22px; font-weight:600; }}
.k {{ fill:#52514e; font-size:11px; }} .grid {{ stroke:#e4e3df; }} .bar {{ fill:#2a78d6; }} .hit {{ fill:none; }}</style>
<rect width="600" height="236" rx="10" fill="#fcfcfb" stroke="#e4e3df"/>
<text x="24" y="30" class="h">Downloads per day, last {len(daily)} days</text>
<text x="576" y="30" text-anchor="end">OP3 · data to {html.escape(s.get("asof") or "?")}</text>
{tiles}
{chart}
</svg>
"""


def render(s: dict) -> None:
    c = cfg()
    (SITE / "stats.html").write_text(page(s, c))
    (SITE / "stats.svg").write_text(card(s))


def main() -> None:
    if sys.argv[1:2] == ["render"]:
        s = json.loads(OUT_JSON.read_text())
    else:
        s = collect()
        OUT_JSON.write_text(json.dumps(s, indent=1) + "\n")
    render(s)
    print(f"stats: {s['monthly_downloads']} downloads in 30 days, {len(s['episodes'])} episodes -> site/stats.html")


if __name__ == "__main__":
    main()
