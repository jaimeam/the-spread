#!/usr/bin/env python3
"""
The Spread: ingest -> tag -> brief, for oil & gas, refining, petchem,
midstream: what moved, and why.

Standard library only (Python 3.11+). No pip install needed.

Usage
  python pipeline.py run                 # ingest + tag + write today's private brief
  python pipeline.py run --public        # only sources marked public_ok = true
  python pipeline.py run --no-llm        # no API key: keyword tagging + plain digest
  python pipeline.py run --fixtures fixtures   # offline test with saved feeds

Choosing models: one setting per task, written as "provider:model".
  TAG_MODEL           tags each news item      default anthropic:claude-haiku-5-5
  BRIEF_MODEL         writes the brief         default anthropic:claude-opus-5-5
  SCRIPT_MODEL        writes the audio script  default: same as BRIEF_MODEL
  Providers: anthropic, mistral. Example: TAG_MODEL=mistral:mistral-small-latest

Keys (only for the providers you use; EIA/AGSI optional)
  ANTHROPIC_API_KEY   console.anthropic.com
  MISTRAL_API_KEY     console.mistral.ai
  EIA_API_KEY         free key: https://www.eia.gov/opendata/register.php
  AGSI_API_KEY        free key: https://agsi.gie.eu (EU gas storage)
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "brief.db"
BRIEF_DIR = ROOT / "briefs"
UA = "the-spread/0.1 (personal research newsletter)"
# `or` (not a default arg) so an empty variable from GitHub falls back too.
TAG_MODEL = os.environ.get("TAG_MODEL") or "anthropic:claude-haiku-5-5"
BRIEF_MODEL = os.environ.get("BRIEF_MODEL") or "anthropic:claude-opus-5-5"
SCRIPT_MODEL = os.environ.get("SCRIPT_MODEL") or BRIEF_MODEL
PROVIDER_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "mistral": "MISTRAL_API_KEY"}
SEGMENTS = ["upstream", "midstream", "refining", "petchem", "gas_lng", "shipping", "geopolitics", "macro"]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- config

def load_toml(name: str) -> dict:
    with open(ROOT / name, "rb") as f:
        return tomllib.load(f)


# --------------------------------------------------------------------------- http

def http_get(url: str, headers: dict | None = None, retries: int = 2) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:  # GDELT rate limit
                time.sleep(6 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- fetchers
# Each returns a list of dicts: title, url, published (ISO, UTC), summary

def _parse_date(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat()


def _clean(text: str | None, limit: int = 600) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0 and Atom."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        out.append({
            "title": _clean(it.findtext("title"), 300),
            "url": (it.findtext("link") or "").strip(),
            "published": _parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")),
            "summary": _clean(it.findtext("description")),
        })
    for e in root.iter(f"{atom}entry"):
        link = e.find(f"{atom}link")
        out.append({
            "title": _clean(e.findtext(f"{atom}title"), 300),
            "url": link.get("href") if link is not None else "",
            "published": _parse_date(e.findtext(f"{atom}updated") or e.findtext(f"{atom}published")),
            "summary": _clean(e.findtext(f"{atom}summary")),
        })
    return [i for i in out if i["title"] and i["url"]]


def fetch_rss(src: dict) -> list[dict]:
    return parse_feed(http_get(src["url"]))


def fetch_google_news(src: dict) -> list[dict]:
    q = urllib.parse.quote(src["query"] + " when:2d")
    return parse_feed(http_get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"))


def fetch_gdelt(src: dict) -> list[dict]:
    q = urllib.parse.quote(src["query"])
    url = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}"
           "&mode=artlist&format=json&maxrecords=50&timespan=1d&sort=datedesc")
    data = json.loads(http_get(url) or "{}")
    out = []
    for a in data.get("articles", []):
        seen = a.get("seendate", "")  # 20261012T081500Z
        pub = None
        if len(seen) >= 15:
            pub = dt.datetime.strptime(seen[:15], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc).isoformat()
        out.append({"title": _clean(a.get("title"), 300), "url": a.get("url", ""),
                    "published": pub, "summary": a.get("domain", "")})
    return out


def parse_ofac(page: str) -> list[dict]:
    """OFAC has no RSS; parse the Recent Actions list."""
    out = []
    for m in re.finditer(
            r'<a[^>]+href="(?:https://ofac\.treasury\.gov)?(/recent-actions/\d{8}[^"]*)"[^>]*>(.*?)</a>.*?'
            r'((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4})',
            page, re.S):
        href, title, date = m.groups()
        pub = dt.datetime.strptime(date, "%B %d, %Y").replace(hour=12, tzinfo=dt.timezone.utc)
        out.append({"title": _clean(title, 400), "url": "https://ofac.treasury.gov" + href,
                    "published": pub.isoformat(), "summary": "OFAC recent action"})
    return out


def fetch_ofac(src: dict) -> list[dict]:
    return parse_ofac(http_get(src["url"]))


FETCHERS = {"rss": fetch_rss, "google_news": fetch_google_news, "gdelt": fetch_gdelt, "ofac": fetch_ofac}


def fetch_from_fixture(src: dict, fixtures: Path) -> list[dict]:
    slug = re.sub(r"[^a-z0-9]+", "_", src["name"].lower()).strip("_")
    for ext in (".xml", ".json", ".html"):
        p = fixtures / f"{slug}{ext}"
        if p.exists():
            text = p.read_text()
            if src["type"] == "ofac":
                return parse_ofac(text)
            if src["type"] == "gdelt":
                return [dict(a, summary=a.get("summary", "")) for a in json.loads(text)]
            return parse_feed(text)
    return []


# --------------------------------------------------------------------------- data series

def eia_weekly() -> list[str]:
    """US weekly balances from the EIA API v2 (free key). Returns text lines."""
    key = os.environ.get("EIA_API_KEY")
    if not key:
        return []
    series = {
        "WCESTUS1": ("petroleum/stoc/wstk", "US commercial crude stocks", "kb"),
        "WGTSTUS1": ("petroleum/stoc/wstk", "US total gasoline stocks", "kb"),
        "WDISTUS1": ("petroleum/stoc/wstk", "US distillate stocks", "kb"),
        "WPULEUS3": ("petroleum/pnp/wiup", "US refinery utilisation", "%"),
    }
    lines = []
    for sid, (route, label, unit) in series.items():
        url = (f"https://api.eia.gov/v2/{route}/data/?api_key={key}&frequency=weekly&data[0]=value"
               f"&facets[series][]={sid}&sort[0][column]=period&sort[0][direction]=desc&length=2")
        try:
            rows = json.loads(http_get(url))["response"]["data"]
            cur, prev = float(rows[0]["value"]), float(rows[1]["value"])
            lines.append(f"{label} (week to {rows[0]['period']}): {cur:,.1f} {unit}, "
                         f"change {cur - prev:+,.1f} {unit} [EIA]")
        except Exception as e:  # noqa: BLE001
            log(f"EIA {sid} failed: {e}")
    return lines


def eu_gas_storage() -> list[str]:
    key = os.environ.get("AGSI_API_KEY")
    if not key:
        return []
    try:
        data = json.loads(http_get("https://agsi.gie.eu/api?type=eu", headers={"x-key": key}))["data"]
        d0, d1 = data[0], data[1]
        return [f"EU gas storage {d0['gasDayStart']}: {float(d0['full']):.1f}% full "
                f"({float(d0['full']) - float(d1['full']):+.2f} pts d/d) [GIE AGSI]"]
    except Exception as e:  # noqa: BLE001
        log(f"AGSI failed: {e}")
        return []


# --------------------------------------------------------------------------- storage

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY, source TEXT, public_ok INTEGER, title TEXT, url TEXT,
        published TEXT, summary TEXT, fetched_at TEXT, tags TEXT)""")
    return con


def ingest(sources: list[dict], fixtures: Path | None) -> int:
    con, new = db(), 0
    for src in sources:
        try:
            items = fetch_from_fixture(src, fixtures) if fixtures else FETCHERS[src["type"]](src)
        except Exception as e:  # noqa: BLE001  one dead feed must not kill the run
            log(f"skip {src['name']}: {e}")
            continue
        for it in items:
            iid = hashlib.sha1((it["url"] or it["title"]).encode()).hexdigest()[:16]
            cur = con.execute(
                "INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,NULL)",
                (iid, src["name"], int(src.get("public_ok", False)), it["title"], it["url"],
                 it["published"] or now_utc().isoformat(), it["summary"], now_utc().isoformat()))
            new += cur.rowcount
        log(f"{src['name']}: {len(items)} items")
        if src["type"] == "gdelt" and not fixtures:
            time.sleep(5)  # be polite to GDELT
    con.commit()
    return new


# --------------------------------------------------------------------------- Claude

def split_model(spec: str) -> tuple[str, str]:
    """'mistral:mistral-large-latest' -> ('mistral', 'mistral-large-latest'). No prefix = anthropic."""
    provider, _, model = spec.partition(":")
    if not model:
        return "anthropic", provider
    if provider not in PROVIDER_KEYS:
        raise SystemExit(f"Unknown provider '{provider}' in '{spec}'. Use one of: {', '.join(PROVIDER_KEYS)}")
    return provider, model


def has_key(spec: str) -> bool:
    return bool(os.environ.get(PROVIDER_KEYS[split_model(spec)[0]]))


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", **headers})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f"{url} -> HTTP {e.code}: {e.read()[:500]!r}") from e
    raise RuntimeError("unreachable")


def llm(prompt: str, spec: str, max_tokens: int = 4000, system: str = "") -> str:
    """Send one prompt to whichever provider `spec` names and return the text."""
    provider, model = split_model(spec)
    key = os.environ[PROVIDER_KEYS[provider]]
    if provider == "anthropic":
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        resp = _post("https://api.anthropic.com/v1/messages", body,
                     {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    # mistral: OpenAI-style chat completions
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    resp = _post("https://api.mistral.ai/v1/chat/completions",
                 {"model": model, "max_tokens": max_tokens, "messages": msgs},
                 {"Authorization": f"Bearer {key}"})
    return resp["choices"][0]["message"]["content"]


claude = llm  # old name, kept so nothing else breaks


def _json_from(text: str):
    m = re.search(r"\[.*\]|\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else None


# --------------------------------------------------------------------------- tagging

def keyword_tag(item: dict, nodes: list[dict]) -> dict:
    text = f"{item['title']} {item['summary']}".lower()
    hits = [n["id"] for n in nodes if any(k in text for k in n["keywords"])]
    seg_words = {
        "refining": ["refin", "crack", "diesel", "distillate", "gasoline", "jet fuel", "cdu", "fcc"],
        "petchem": ["petrochem", "cracker", "ethylene", "propylene", "naphtha", "polymer"],
        "midstream": ["pipeline", "terminal", "storage", "midstream"],
        "upstream": ["opec", "production", "drilling", "rig", "upstream"],
        "gas_lng": ["lng", "natural gas", "ttf", "henry hub"],
        "shipping": ["tanker", "vessel", "freight", "shipping"],
        "geopolitics": ["sanction", "war", "attack", "missile", "ofac", "ceasefire", "strike"],
    }
    segs = [s for s, ws in seg_words.items() if any(w in text for w in ws)]
    return {"segments": segs, "nodes": hits, "relevance": min(5, len(hits) * 2 + len(segs)),
            "why": ""}


TAG_PROMPT = """You tag news items for a professional oil, gas, refining, petrochemical and
midstream newsletter that focuses on how geopolitics moves physical markets.

For each item return an object with:
  id         (copy it)
  segments   subset of {segments}
  nodes      ids from this exposure map that the item touches: {nodes}
  relevance  0-5 (5 = moves prices/flows/margins now; 0 = off-topic, PR fluff, duplicate)
  why        <= 20 words: the market mechanism, not a summary

Return ONLY a JSON array.

Items:
{items}"""


def tag(nodes: list[dict], use_llm: bool) -> int:
    con = db()
    rows = con.execute("SELECT id,title,summary,source FROM items WHERE tags IS NULL").fetchall()
    if not rows:
        return 0
    if not use_llm:
        for iid, title, summary, _ in rows:
            con.execute("UPDATE items SET tags=? WHERE id=?",
                        (json.dumps(keyword_tag({"title": title, "summary": summary}, nodes)), iid))
        con.commit()
        return len(rows)
    node_ids = ", ".join(n["id"] for n in nodes)
    for i in range(0, len(rows), 25):
        batch = rows[i:i + 25]
        items = "\n".join(json.dumps({"id": r[0], "title": r[1], "summary": r[2][:300], "source": r[3]})
                          for r in batch)
        try:
            out = _json_from(claude(TAG_PROMPT.format(segments=SEGMENTS, nodes=node_ids, items=items),
                                    TAG_MODEL, 4000)) or []
        except Exception as e:  # noqa: BLE001
            log(f"tagging batch failed ({e}); falling back to keywords")
            out = []
        by_id = {o.get("id"): o for o in out if isinstance(o, dict)}
        for iid, title, summary, _ in batch:
            t = by_id.get(iid) or keyword_tag({"title": title, "summary": summary}, nodes)
            con.execute("UPDATE items SET tags=? WHERE id=?", (json.dumps(t), iid))
        con.commit()
    return len(rows)


# --------------------------------------------------------------------------- brief

BRIEF_SYSTEM = """You are the editor of a daily brief on oil & gas, refining, petrochemicals,
midstream, and the events that move them. The reader is technical and busy.

Rules
- Lead with what changed in the last 24h and why it matters for flows, margins or prices.
- For every major story give the transmission chain: event -> mechanism -> who feels it.
  Use the exposure map; add links it is missing if the evidence supports them.
- Separate fact from claim. If only one side says something (e.g. a politician announces
  a deal the other party has not confirmed), say so.
- Never invent numbers. Use only figures present in the items or data block, and name the
  source in brackets after each figure.
- Paraphrase. No quotes longer than 10 words. Every story ends with its source link(s).
- If a story only appears in headline-only aggregators, mark it "(unconfirmed - headline only)".
- Close with "Watch next" (3-5 dated items) and "Signals I'm not sure about" (1-3 lines).
- Markdown. Start with the heading "# The Spread - <weekday, date>". Target 600-900 words."""


def recent_items(hours: int, public: bool, min_rel: int = 2) -> list[dict]:
    since = (now_utc() - dt.timedelta(hours=hours)).isoformat()
    q = "SELECT source,title,url,published,summary,tags,public_ok FROM items WHERE published >= ?"
    rows = db().execute(q + (" AND public_ok=1" if public else ""), (since,)).fetchall()
    out = []
    for source, title, url, pub, summary, tags, pok in rows:
        t = json.loads(tags or "{}")
        if t.get("relevance", 0) >= min_rel:
            out.append({"source": source, "title": title, "url": url, "published": pub,
                        "summary": summary, **t})
    out.sort(key=lambda x: x["published"], reverse=True)          # newest first...
    return sorted(out, key=lambda x: -x.get("relevance", 0))       # ...within relevance


def write_brief(nodes: list[dict], hours: int, public: bool, use_llm: bool) -> Path:
    items = recent_items(hours, public)
    data = eia_weekly() + eu_gas_storage()
    today = dt.date.today().isoformat()
    BRIEF_DIR.mkdir(exist_ok=True)
    path = BRIEF_DIR / f"{today}{'-public' if public else ''}.md"

    if not use_llm:
        lines = [f"# The Spread - {today} (keyword digest, no LLM)", ""]
        if data:
            lines += ["## Data", *[f"- {d}" for d in data], ""]
        for seg in SEGMENTS:
            seg_items = [i for i in items if seg in i.get("segments", [])]
            if seg_items:
                lines.append(f"## {seg.replace('_', ' ').title()}")
                lines += [f"- [{i['title']}]({i['url']}) - {i['source']}" for i in seg_items[:12]]
                lines.append("")
        path.write_text("\n".join(lines))
        return path

    map_txt = "\n".join(f"- {n['label']} ({n['id']}): " + " | ".join(n["chain"]) for n in nodes)
    items_txt = "\n".join(json.dumps({k: i.get(k) for k in
                                      ("title", "url", "source", "published", "summary", "nodes", "why", "relevance")})
                          for i in items[:120])
    prompt = (f"Date: {today}. Edition: {'PUBLIC (free/primary sources only)' if public else 'PRIVATE'}.\n\n"
              f"## Exposure map\n{map_txt}\n\n## Data\n" + ("\n".join(data) or "(none today)") +
              f"\n\n## Items from the last {hours}h ({len(items)})\n{items_txt}\n\nWrite today's brief.")
    path.write_text(claude(prompt, BRIEF_MODEL, 6000, BRIEF_SYSTEM))
    return path


# --------------------------------------------------------------------------- cli

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "ingest", "tag", "brief"])
    ap.add_argument("--public", action="store_true", help="only public_ok sources")
    ap.add_argument("--no-llm", action="store_true", help="keyword tagging, plain digest")
    ap.add_argument("--hours", type=int, default=30)
    ap.add_argument("--fixtures", type=Path, help="read saved feeds from this folder instead of the web")
    a = ap.parse_args()

    use_llm = not a.no_llm
    missing = {PROVIDER_KEYS[split_model(m)[0]] for m in (TAG_MODEL, BRIEF_MODEL) if not has_key(m)}
    if use_llm and missing:
        log(f"{', '.join(sorted(missing))} not set -> running with --no-llm")
        use_llm = False
    if use_llm:
        log(f"models: tag={TAG_MODEL} brief={BRIEF_MODEL}")

    sources = [s for s in load_toml("sources.toml")["source"] if s.get("public_ok") or not a.public]
    nodes = load_toml("exposure_map.toml")["node"]

    if a.cmd in ("run", "ingest"):
        log(f"ingested {ingest(sources, a.fixtures)} new items")
    if a.cmd in ("run", "tag"):
        log(f"tagged {tag(nodes, use_llm)} items")
    if a.cmd in ("run", "brief"):
        log(f"brief written to {write_brief(nodes, a.hours, a.public, use_llm)}")


if __name__ == "__main__":
    main()
