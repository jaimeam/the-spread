"""Listener stats from OP3 (op3.dev) -> site/stats.json, site/stats-history.json, site/stats.html, site/stats.svg.

    python stats.py           # fetch from OP3 (needs OP3_TOKEN), then render
    python stats.py render    # re-render the page and card from the saved JSON, offline

Episode MP3 links from `op3_from` (podcast.toml) on go through op3.dev, which logs every request.
The single data source is OP3's download log for the show, /downloads/show/{showUuid}: one request
returns the whole history (it pages with a continuationToken only past `limit`). The showUuid is
OP3's own id, not the feed's podcast:guid, so every run looks it up first via /shows/{guid}.
OP3 builds that log in daily batches, so it runs a day or two behind; every window on the page ends
at OP3's `asof` day, not at the time of the run. (/hits, the raw request log, is not used: it caps
at 100 rows, has no paging and ignores from/to/since.)

Each run recounts every day the download log covers and writes it into site/stats-history.json.
Days older than the log's first row (recorded earlier from /hits) are kept exactly as they are.
OP3 only sees apps that fetch our MP3 link (Apple Podcasts and other direct clients). Spotify
serves its own copy, so Spotify listens never reach OP3.

Only aggregates are saved, never per-download rows, hashed IPs or audience ids. Standard library only.
"""

import html
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
OUT_JSON = SITE / "stats.json"
HISTORY = SITE / "stats-history.json"
API = "https://op3.dev/api/1"
PAGE = 1000  # rows per /downloads request; more pages follow via continuationToken
TOP = 8                    # apps / countries listed
CARD_DAYS = 30


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


def lookup_show(c: dict, fetch=get) -> dict:
    """OP3's show record. The podcast:guid in our feed and OP3's showUuid are different ids, and
    every query needs the showUuid, so it is looked up on each run rather than stored."""
    try:
        return fetch(f"/shows/{c['guid']}")
    except urllib.error.HTTPError as e:
        sys.exit(f"stats: OP3 has no show for podcast guid {c['guid']} ({e.code})")


def downloads(uuid: str, fetch=get) -> list[dict]:
    """The show's whole download log. One page holds everything today; a continuationToken only
    appears when a page is cut at `limit`, and is followed if it does."""
    rows, token = [], None
    while True:
        params = {"format": "json", "limit": PAGE}
        if token:
            params["continuationToken"] = token
        page = fetch(f"/downloads/show/{uuid}", **params)
        rows += page.get("rows", [])
        token = page.get("continuationToken")
        if not token:
            return rows


def show_counts(uuid: str, fetch=get) -> dict:
    """OP3's own totals (show-download-counts): a cross-check, and `asof`, the last day OP3 has built."""
    try:
        return fetch("/queries/show-download-counts", showUuid=uuid)
    except urllib.error.HTTPError as e:
        print(f"stats: warning: OP3 show-download-counts failed ({e.code}); no cross-check")
        return {}


# --------------------------------------------------------------------------- count

# Downloads that aren't someone listening, matched on OP3's agentName / agentType. iTMS/itms is
# Apple checking the episode for its directory; the rest are crawlers, scripts and bare HTTP
# libraries. Same list as the morning message, so both count the same downloads.
BOTS = [(re.compile(r"itms", re.I), "Apple (directory check)"),
        (re.compile(r"bot|crawl|spider|curl|wget|python|guzzle|okhttp|go-http|axios|node-fetch|"
                    r"java/|libwww|httpclient|feedfetcher|podscan", re.I), "Other bots and scripts")]
APPS = {"AppleCoreMedia": "Apple Podcasts", "PodcastAddict": "Podcast Addict", "PocketCasts": "Pocket Casts",
        "CastBox": "Castbox", "PodcastRepublic": "Podcast Republic", "iHeart": "iHeartRadio",
        "AmazonMusic": "Amazon Music"}


def bot(name: str, kind: str) -> str | None:
    return next((label for rx, label in BOTS if rx.search(name) or rx.search(kind)), None)


def app(name: str, kind: str) -> str:
    if kind == "browser":
        return "Web browser"
    return APPS.get(name, name or "Unknown")


def count(rows: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """One download per hashedIpAddress + agentName + episode + UTC day (episode = the first 10
    characters of OP3's episodeId), bots excluded: the same rule as the morning message.
    Returns (downloads, [(bot kind, time)])."""
    seen, out, bots = set(), [], []
    for r in rows:
        name, kind = r.get("agentName") or "", r.get("agentType") or ""
        if label := bot(name, kind):
            bots.append((label, r["time"]))
            continue
        day = r["time"][:10]
        key = (r.get("hashedIpAddress"), name, (r.get("episodeId") or "")[:10], day)
        if key in seen:
            continue
        seen.add(key)
        # The page names episodes by date, taken from the MP3 name (DATE.mp3 or DATE-<unix>.mp3).
        ep = (r.get("url") or "").rsplit("/", 1)[-1][:10]
        out.append({"time": r["time"], "day": day, "episode": ep, "app": app(name, kind),
                    "country": r.get("countryCode") or "??", "listener": r.get("audienceId")})
    return out, bots


def per_day(dls: list[dict]) -> dict:
    days = defaultdict(lambda: {"downloads": 0, "episodes": Counter(), "apps": Counter(), "countries": Counter()})
    for d in dls:
        x = days[d["day"]]
        x["downloads"] += 1
        x["episodes"][d["episode"]] += 1
        x["apps"][d["app"]] += 1
        x["countries"][d["country"]] += 1
    return days


def merge(history: dict, days: dict, first: date, last: date) -> dict:
    """Write every day from `first` (the log's first day) to `last` (OP3's asof) into the history,
    recounted from the log, zero if nobody downloaded. Days outside that span are left as they are."""
    out = dict(history)
    d = first
    while d <= last:
        k = d.isoformat()
        x = days.get(k, {"downloads": 0, "episodes": {}, "apps": {}, "countries": {}})
        out[k] = {"downloads": x["downloads"], **{f: dict(sorted(x[f].items())) for f in ("episodes", "apps", "countries")}}
        d += timedelta(1)
    return dict(sorted(out.items()))


def collect(fetch=get, now: datetime | None = None) -> tuple[dict, dict]:
    c = cfg()
    now = now or datetime.now(timezone.utc)
    show = lookup_show(c, fetch)
    uuid = show["showUuid"]
    rows = downloads(uuid, fetch)
    q = show_counts(uuid, fetch)
    op3 = q.get("showDownloadCounts", {}).get(uuid, {})
    dls, bots = count(rows)

    # Windows end with OP3's last built day, not with the run: the log runs a day or two behind.
    last_row = max((r["time"][:10] for r in rows), default=None)
    asof = date.fromisoformat(max(filter(None, [q.get("asof"), last_row]), default=now.date().isoformat()))
    end = datetime.combine(asof + timedelta(1), datetime.min.time(), timezone.utc)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S")
    s24, s7, e = iso(end - timedelta(days=1)), iso(end - timedelta(days=7)), iso(end)
    d24 = [d for d in dls if s24 <= d["time"] < e]
    d7 = [d for d in dls if s7 <= d["time"] < e]

    history = json.loads(HISTORY.read_text())["days"] if HISTORY.exists() else {}
    if last_row:
        history = merge(history, per_day(dls), date.fromisoformat(min(r["time"][:10] for r in rows)), asof)

    by_ep_24, by_ep_7 = Counter(d["episode"] for d in d24), Counter(d["episode"] for d in d7)
    titles = {e["date"]: e["title"] for e in json.loads((SITE / "episodes.json").read_text())}
    eps = sorted({e for e in titles if e >= c.get("op3_from", "")} | set(by_ep_7), reverse=True)
    month = sum(1 for d in dls if iso(end - timedelta(days=30)) <= d["time"] < e)
    if op3.get("monthlyDownloads") is not None and op3["monthlyDownloads"] != month:
        print(f"stats: note: OP3 counts {op3['monthlyDownloads']} downloads in 30 days, this page {month}")
    stats = {
        "updated": now.strftime("%Y-%m-%dT%H:%MZ"),
        "asof": asof.isoformat(),
        "window": [(end - timedelta(days=7)).strftime("%Y-%m-%dT%H:%MZ"), end.strftime("%Y-%m-%dT%H:%MZ")],
        "op3_from": c.get("op3_from"),
        "downloads_24h": len(d24),
        "downloads_7d": len(d7),
        "listeners_7d": len({d["listener"] for d in d7 if d["listener"]}),
        "countries_7d": dict(Counter(d["country"] for d in d7).most_common()),
        "bots_7d": dict(Counter(k for k, t in bots if s7 <= t < e).most_common()),
        "episodes": [{"date": ep, "title": titles.get(ep, ""), "downloads_24h": by_ep_24[ep],
                      "downloads_7d": by_ep_7[ep]} for ep in eps],
        "op3_show": {"uuid": uuid, "page": show.get("statsPageUrl") or f"https://op3.dev/show/{uuid}",
                     "monthly_downloads": op3.get("monthlyDownloads"),
                     "weekly_avg_downloads": op3.get("weeklyAvgDownloads")},
    }
    return stats, {"note": "Downloads per UTC day counted by stats.py from OP3's download log (one per IP + app + "
                           "episode + day, bots excluded). Days the log covers are recounted each run; "
                           "older days, recorded from OP3's hit log, are kept as they were.", "days": history}

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
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:24px; }
.tile { border:1px solid var(--grid); border-radius:8px; padding:14px 16px; }
.tile .v { font-size:30px; font-weight:600; font-variant-numeric:tabular-nums; }
.tile .k { color:var(--ink-2); font-size:13px; }
svg text { fill:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
svg .grid { stroke:var(--grid); stroke-width:1; }
svg .bar { fill:var(--bar); } svg g.col:hover .bar { fill:var(--bar-hover); }
svg .hit { fill:transparent; } svg .bar.today { opacity:.5; } svg .gap { stroke:var(--muted); stroke-dasharray:2 2; }
svg .tip { opacity:0; pointer-events:none; } svg g.col:hover .tip { opacity:1; }
svg .tip text { fill:var(--ink); font-size:12px; }
svg .tip rect { fill:var(--surface); stroke:var(--grid); }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { text-align:right; padding:6px 8px; border-bottom:1px solid var(--grid); white-space:nowrap; }
th { color:var(--ink-2); font-weight:500; font-size:13px; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align:left; }
td:nth-child(2) { white-space:normal; min-width:16em; }
.chart { overflow-x:auto; } .chart svg { min-width:600px; }
.two { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:32px; }
.row { display:grid; grid-template-columns:minmax(8.5em,1fr) 3fr auto; gap:10px; align-items:center; margin:6px 0; }
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


def end_day(s: dict) -> str:
    """Last day the numbers cover: OP3's asof (older saved files: the day of the run)."""
    return s.get("asof") or s["updated"][:10]


def series(days: dict, today: str, current: str | None = None) -> list[dict]:
    """Every day from the first recorded one to `today` (the last day covered). A day the history
    doesn't have stays None: no bar, marked as not recorded. `current`, if given, is drawn as
    still counting."""
    if not days:
        return []
    d, end, out = date.fromisoformat(min(days)), date.fromisoformat(max(max(days), today)), []
    while d <= end:
        k = d.isoformat()
        out.append({"date": k, "downloads": days[k]["downloads"] if k in days else None, "today": k == current})
        d += timedelta(1)
    return out


def daily_svg(daily: list[dict], width=920, height=220, tips=True) -> str:
    """Column chart of downloads per day. One series: no legend, the heading names it.
    Today (still counting) is drawn lighter; a day with no record gets a dashed baseline."""
    left, right, top, bottom = 34, 8, 10, 24
    w, h = width - left - right, height - top - bottom
    ymax = nice_max(max([d["downloads"] or 0 for d in daily] + [1]))
    n = max(len(daily), 1)
    slot = w / max(n, 7)  # a short history keeps sensible column widths, aligned left
    bw = min(24, max(2, slot - 2))  # 2px gap between adjacent columns, 24px cap
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
           f'aria-label="Downloads per day, {n} days">']
    for i in range(5):
        v = ymax * i / 4
        y = top + h - h * i / 4
        out.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{v:,.0f}</text>')
    step = max(1, n // 6)
    for i, d in enumerate(daily):
        x = left + i * slot + (slot - bw) / 2
        v = d["downloads"]
        bh = h * (v or 0) / ymax
        y = top + h - bh
        r = min(4, bw / 2, bh)
        # Rounded at the data end, square at the baseline.
        path = (f"M{x:.1f},{top + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
                f"H{x + bw - r:.1f}Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f}V{top + h:.1f}Z") if bh else ""
        what = "not recorded" if v is None else f"{v:,} downloads" + (" so far" if d.get("today") else "")
        label = f'{date.fromisoformat(d["date"]):%a %d %b}: {what}'
        out.append(f'<g class="col"><title>{label}</title>'
                   f'<rect class="hit" x="{left + i * slot:.1f}" y="{top}" width="{slot:.1f}" height="{h}"/>'
                   + (f'<path class="bar{" today" if d.get("today") else ""}" d="{path}"/>' if path else "")
                   + (f'<line class="gap" x1="{x:.1f}" x2="{x + bw:.1f}" y1="{top + h - 1:.1f}" y2="{top + h - 1:.1f}"/>'
                      if v is None else ""))
        if tips:
            tx = min(max(x + bw / 2, left + 80), width - right - 80)
            out.append(f'<g class="tip"><rect x="{tx - 78:.1f}" y="{top}" width="156" height="22" rx="4"/>'
                       f'<text x="{tx:.1f}" y="{top + 15}" text-anchor="middle">{html.escape(label.replace(" downloads", ""))}</text></g>')
        out.append("</g>")
        if i % step == 0 or i == n - 1:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{height - 6}" text-anchor="middle">'
                       f'{date.fromisoformat(d["date"]):%d %b}</text>')
    out.append("</svg>")
    return "".join(out)


def bar_list(pairs: list) -> str:
    if not pairs:
        return '<p class="note">No data yet.</p>'
    top = max(n for _, n in pairs)
    return "".join(
        f'<div class="row"><span class="name">{html.escape(k)}</span>'
        f'<span class="track"><span class="fill" style="display:block;width:{100 * n / top:.1f}%"></span></span>'
        f'<span class="n">{n:,}</span></div>' for k, n in pairs)


def totals(days: dict, field: str) -> Counter:
    out = Counter()
    for x in days.values():
        out.update(x[field])
    return out


def span(daily: list[dict]) -> str:
    """'25 Oct – 31 Oct (7 days)', the real reach of the history."""
    if not daily:
        return "nothing recorded yet"
    a, b = (date.fromisoformat(daily[i]["date"]) for i in (0, -1))
    return f"{a:%-d %b} – {b:%-d %b %Y} ({len(daily)} day{'s' * (len(daily) != 1)})"


SOURCE_NOTE = ("OP3 counts the apps that fetch the episode from our link: Apple Podcasts and other "
               "podcast apps, and browsers. Spotify serves its own copy of each episode, so Spotify "
               "listens don't reach OP3 and aren't counted here.")


def page(s: dict, h: dict, c: dict) -> str:
    title = html.escape(c["title"])
    days = h["days"]
    daily = series(days, end_day(s))
    since = daily[0]["date"] if daily else None
    since_label = f"{date.fromisoformat(since):%-d %b}" if since else "?"
    all_eps = totals(days, "episodes")
    missing = sum(d["downloads"] is None for d in daily)
    eps = "".join(
        f'<tr><td>{html.escape(e["date"])}</td><td>{html.escape(e["title"].rsplit(" | ", 1)[0])}</td>'
        f'<td>{fmt(e["downloads_24h"])}</td><td>{fmt(e["downloads_7d"])}</td><td>{fmt(all_eps.get(e["date"], 0))}</td></tr>'
        for e in s["episodes"])
    asof = f"{date.fromisoformat(end_day(s)):%-d %b}"
    countries = (Counter(s["countries_7d"]) if "countries_7d" in s else totals(days, "countries")).most_common(TOP)
    tiles = [(f"Downloads on {asof} (last day OP3 has)", s["downloads_24h"]),
             (f"Downloads, 7 days to {asof}", s["downloads_7d"]),
             (f"Listeners, 7 days to {asof}", s["listeners_7d"]),
             (f"Downloads since {since_label}", sum(d["downloads"] or 0 for d in daily))]
    show = s.get("op3_show")
    if show:
        tiles += [("OP3: downloads, last 30 days", show["monthly_downloads"]),
                  ("OP3: average per week", show["weekly_avg_downloads"])]
    op3 = html.escape(show["page"]) if show else "https://op3.dev"
    bots = ", ".join(f"{k} {v:,}" for k, v in s["bots_7d"].items()) or "none"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Listener stats · {title}</title>
<style>{CSS}</style></head>
<body><main>
<h1>Listener stats</h1>
<p class="sub">{title}. Downloads logged by <a href="{op3}">OP3</a>, the open podcast analytics service,
bots excluded. Counts run to {asof} (OP3 adds each day's downloads a day or two later);
page updated {html.escape(s["updated"])}.</p>
<p class="note">{SOURCE_NOTE}</p>
<div class="tiles">{"".join(f'<div class="tile"><div class="v">{fmt(v)}</div><div class="k">{k}</div></div>' for k, v in tiles)}</div>

<h2>Downloads per day</h2>
{f'<div class="chart">{daily_svg(daily)}</div>' if daily else '<p class="note">No days recorded yet.</p>'}
<p class="note">Recorded: {span(daily)}{f"; {missing} day{'s' * (missing != 1)} were not recorded" if missing else ""}.
Hover a column for the day's count.</p>

<h2>Episodes</h2>
<div style="overflow-x:auto"><table><thead><tr><th>Date</th><th>Episode</th><th>{asof}</th><th>7 days</th><th>Since {since_label}</th></tr></thead>
<tbody>{eps or '<tr><td colspan="5">No episodes counted yet.</td></tr>'}</tbody></table></div>

<div class="two">
<section><h2>Apps</h2>{bar_list(totals(days, "apps").most_common(TOP))}</section>
<section><h2>Countries, 7 days</h2>{bar_list(countries)}</section>
</div>
<p class="note">Apps cover {span(daily)}; countries the 7 days to {asof}.</p>

<p class="note" style="margin-top:40px">How it's counted: a download is one in OP3's download log
from a podcast app or browser, once per app, network and episode per UTC day. Not counted: automated
downloads, in the 7 days: {html.escape(bots)}. Listeners are OP3's distinct audience ids (an estimate:
OP3 derives them from network and app). Only episodes from
{html.escape(s.get("op3_from") or "?")} on are counted: older episodes keep their original MP3 link.
Data: <a href="stats.json">stats.json</a> · <a href="stats-history.json">stats-history.json</a> ·
<a href="feed.xml">podcast feed</a>.</p>
</main></body></html>
"""


def card(s: dict, h: dict) -> str:
    """Preview image for the README: headline numbers + up to the last 30 recorded days. Light colors
    only (GitHub shows README images on both themes, so the card brings its own background)."""
    daily = series(h["days"], end_day(s))[-CARD_DAYS:]
    chart = daily_svg(daily, width=560, height=130, tips=False)
    chart = chart.replace("<svg ", '<svg x="20" y="88" width="560" height="130" ', 1).replace(' width="100%"', "")
    since = f"{date.fromisoformat(daily[0]['date']):%-d %b}" if daily else "?"
    nums = [(s["downloads_24h"], f"downloads, {date.fromisoformat(end_day(s)):%-d %b}"), (s["downloads_7d"], "downloads, 7 days"),
            (s["listeners_7d"], "listeners, 7 days"), (sum(d["downloads"] or 0 for d in daily), f"since {since}")]
    tiles = "".join(f'<text x="{24 + i * 142}" y="62" class="v">{fmt(v)}</text>'
                    f'<text x="{24 + i * 142}" y="80" class="k">{k}</text>' for i, (v, k) in enumerate(nums))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 236" width="600" height="236" role="img" aria-label="Podcast downloads: {fmt(s["downloads_7d"])} in the last 7 days">
<style>text {{ font-family:system-ui,-apple-system,'Segoe UI',sans-serif; fill:#8a8984; font-size:11px; }}
.h {{ fill:#0b0b0b; font-size:15px; font-weight:600; }} .v {{ fill:#0b0b0b; font-size:22px; font-weight:600; }}
.k {{ fill:#52514e; font-size:11px; }} .grid {{ stroke:#e4e3df; }} .bar {{ fill:#2a78d6; }} .bar.today {{ opacity:.5; }}
.gap {{ stroke:#8a8984; stroke-dasharray:2 2; }} .hit {{ fill:none; }}</style>
<rect width="600" height="236" rx="10" fill="#fcfcfb" stroke="#e4e3df"/>
<text x="24" y="30" class="h">Downloads per day, {len(daily)} day{"s" * (len(daily) != 1)} recorded</text>
<text x="576" y="30" text-anchor="end">OP3, excl. Spotify · to {html.escape(end_day(s))}</text>
{tiles}
{chart}
</svg>
"""


def render(s: dict, h: dict) -> None:
    c = cfg()
    (SITE / "stats.html").write_text(page(s, h, c))
    (SITE / "stats.svg").write_text(card(s, h))


def dump(obj) -> str:
    return json.dumps(obj, indent=1) + "\n"


def main() -> None:
    if sys.argv[1:2] == ["render"]:
        s, h = json.loads(OUT_JSON.read_text()), json.loads(HISTORY.read_text())
    else:
        s, h = json.loads(json.dumps(collect()))  # same shapes as the saved files, for the comparison below
        old = json.loads(OUT_JSON.read_text()) if OUT_JSON.exists() else {}
        old_h = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
        if old.get("updated") and {**old, "updated": s["updated"], "window": s["window"]} == s and old_h == h:
            s["updated"], s["window"] = old["updated"], old["window"]  # same numbers: leave the files alone
        OUT_JSON.write_text(dump(s))
        HISTORY.write_text(dump(h))
    render(s, h)
    print(f"stats: {s['downloads_24h']} downloads in 24 h, {s['downloads_7d']} in 7 days, "
          f"{s['listeners_7d']} listeners; history {span(series(h['days'], end_day(s)))} -> site/stats.html")


if __name__ == "__main__":
    main()
