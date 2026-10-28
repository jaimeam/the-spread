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
  python pipeline.py run --require-llm   # fail if a model key is missing (used in CI)
  python pipeline.py redo --public --date 2026-10-13   # rewrite that brief from the same items, no fetch

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
DB_PATH = ROOT / (os.environ.get("BRIEF_DB") or "brief.db")  # CI uses its own, committed DB
BRIEF_DIR = ROOT / "briefs"
PROMPTS_DIR = ROOT / "prompts"  # every LLM prompt lives here, one file each
UA = "the-spread/0.1 (personal research newsletter)"
# `or` (not a default arg) so an empty variable from GitHub falls back too.
TAG_MODEL = os.environ.get("TAG_MODEL") or "anthropic:claude-haiku-5-5"
BRIEF_MODEL = os.environ.get("BRIEF_MODEL") or "anthropic:claude-opus-5-5"
SCRIPT_MODEL = os.environ.get("SCRIPT_MODEL") or BRIEF_MODEL
PROVIDER_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "mistral": "MISTRAL_API_KEY"}
# On a safety decline these re-run the request on another model in the same call.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
SEGMENTS = ["upstream", "midstream", "refining", "petchem", "gas_lng", "shipping", "geopolitics", "macro"]


def prompt(name: str, **fields) -> str:
    """Load prompts/<name>.md and fill its {placeholders}."""
    text = (PROMPTS_DIR / f"{name}.md").read_text().rstrip("\n")
    return text.format(**fields) if fields else text


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    """A problem the owner should see: on GitHub Actions it shows on the run's summary page."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{msg}", file=sys.stderr)
    log(f"WARNING {msg}")


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
    # When throttled, GDELT answers 200 with a plain-text "Please limit requests..." note.
    for attempt in range(3):
        text = http_get(url)
        if not text.strip() or text.lstrip().startswith("{"):
            break
        time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f"rate-limited: {text.strip()[:80]}")
    data = json.loads(text or "{}")
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


def parse_palladian(page: str, base: str) -> list[dict]:
    """Palladian sites (LNG Industry, Hydrocarbon Engineering) have no RSS since their Oct 2026
    relaunch; parse the /news/ page. Story links are /<section>/<DDMMYYYY>/<slug>/: the cards carry
    the title as link text plus an excerpt, the header's latest stories only an image alt."""
    found = {}
    for m in re.finditer(r'<a[^>]+href="(/[a-z0-9-]+/(\d{8})/[^"/]+/)"[^>]*>([^<]+)</a>'
                         r'(?:\s*</h\d>\s*<p class="article-card-excerpt">(.*?)</p>)?', page, re.S):
        found.setdefault(m.group(1), (m.group(2), m.group(3), m.group(4)))
    for m in re.finditer(r'href="(/[a-z0-9-]+/(\d{8})/[^"/]+/)"[^>]*>\s*(?:<[^>]+>\s*)*<img[^>]+alt="([^"]+)"', page):
        found.setdefault(m.group(1), (m.group(2), m.group(3), None))
    out = []
    for href, (date, title, excerpt) in found.items():
        try:
            pub = dt.datetime.strptime(date, "%d%m%Y").replace(hour=12, tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        out.append({"title": _clean(title, 300), "url": base.rstrip("/") + href,
                    "published": pub.isoformat(), "summary": _clean(excerpt)})
    return out


def fetch_palladian(src: dict) -> list[dict]:
    base = urllib.parse.urlsplit(src["url"])
    return parse_palladian(http_get(src["url"]), f"{base.scheme}://{base.netloc}")


FETCHERS = {"rss": fetch_rss, "google_news": fetch_google_news, "gdelt": fetch_gdelt, "ofac": fetch_ofac,
            "palladian": fetch_palladian}


def fetch_from_fixture(src: dict, fixtures: Path) -> list[dict]:
    slug = re.sub(r"[^a-z0-9]+", "_", src["name"].lower()).strip("_")
    for ext in (".xml", ".json", ".html"):
        p = fixtures / f"{slug}{ext}"
        if p.exists():
            text = p.read_text()
            if src["type"] == "ofac":
                return parse_ofac(text)
            if src["type"] == "palladian":
                base = urllib.parse.urlsplit(src["url"])
                return parse_palladian(text, f"{base.scheme}://{base.netloc}")
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
    # Which items each edition has already used, so they never appear twice.
    con.execute("""CREATE TABLE IF NOT EXISTS used (
        item_id TEXT, edition TEXT, brief_date TEXT, PRIMARY KEY (item_id, edition))""")
    # Short summaries of past briefs: the brief writer's memory of what it already said.
    con.execute("""CREATE TABLE IF NOT EXISTS ledger (
        brief_date TEXT, edition TEXT, brief_hash TEXT, summary TEXT, PRIMARY KEY (brief_date, edition))""")
    # The data block and look-back each brief was written with, so a redo can use the same.
    con.execute("""CREATE TABLE IF NOT EXISTS inputs (
        brief_date TEXT, edition TEXT, data TEXT, hours INTEGER, PRIMARY KEY (brief_date, edition))""")
    return con


def ingest(sources: list[dict], fixtures: Path | None) -> int:
    """Fetch every source into the items table. Dead, empty or overflowing feeds are warned about:
    a feed that drops stories before we poll it again loses news without any error."""
    con, new = db(), 0
    for src in sources:
        try:
            items = fetch_from_fixture(src, fixtures) if fixtures else FETCHERS[src["type"]](src)
        except Exception as e:  # noqa: BLE001  one dead feed must not kill the run
            warn(f"skip {src['name']}: {e}")
            continue
        # Newest item we already had from this source, before this fetch.
        last_seen = con.execute("SELECT max(published) FROM items WHERE source=?", (src["name"],)).fetchone()[0]
        added = 0
        for it in items:
            iid = hashlib.sha1((it["url"] or it["title"]).encode()).hexdigest()[:16]
            cur = con.execute(
                "INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,NULL)",
                (iid, src["name"], int(src.get("public_ok", False)), it["title"], it["url"],
                 it["published"] or now_utc().isoformat(), it["summary"], now_utc().isoformat()))
            added += cur.rowcount
        new += added
        log(f"{src['name']}: {len(items)} items, {added} new")
        dated = sorted(i["published"] for i in items if i["published"])
        if not items and not fixtures:
            warn(f"{src['name']}: feed returned no items")
        elif items and added == len(items) and last_seen and dated and dated[0] > last_seen:
            # Nothing overlaps with what we had: anything published in between never reached us.
            warn(f"{src['name']}: possible gap, all {len(items)} items are new and the oldest ({dated[0][:16]}) "
                 f"is after the last one seen ({last_seen[:16]}); poll it more often")
        if src["type"] == "gdelt" and not fixtures:
            time.sleep(6)  # GDELT allows one request every 5 seconds
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


def llm(prompt: str, spec: str, max_tokens: int = 16000, system: str = "", effort: str = "medium") -> str:
    """Send one prompt to whichever provider `spec` names and return the text.

    Current Claude models always think first, and thinking counts against max_tokens,
    so keep it generous (you pay only for what's used). `effort` sets how hard it thinks.
    A truncated or refused answer raises rather than being written out half-finished.
    """
    provider, model = split_model(spec)
    key = os.environ[PROVIDER_KEYS[provider]]
    if provider == "anthropic":
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}],
                "output_config": {"effort": effort}}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if system:
            body["system"] = system
        if model in FALLBACK_MODELS:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        resp = _post("https://api.anthropic.com/v1/messages", body, headers)
        u = resp.get("usage", {})
        log(f"{spec}: {u.get('input_tokens', 0):,} in / {u.get('output_tokens', 0):,} out tokens")
        if resp.get("stop_reason") in ("max_tokens", "refusal"):
            raise RuntimeError(f"{spec} stopped early: {resp['stop_reason']} {resp.get('stop_details') or ''}")
        return "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    # mistral: OpenAI-style chat completions
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    resp = _post("https://api.mistral.ai/v1/chat/completions",
                 {"model": model, "max_tokens": max_tokens, "messages": msgs},
                 {"Authorization": f"Bearer {key}"})
    choice = resp["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError(f"{spec} stopped early: hit max_tokens")
    return choice["message"]["content"]



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
            out = _json_from(llm(prompt("tag", segments=SEGMENTS, nodes=node_ids, items=items),
                                    TAG_MODEL, 8000, effort="low")) or []
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



def edition(public: bool) -> str:
    return "public" if public else "private"


def recent_items(hours: int, public: bool, min_rel: int = 2) -> list[dict]:
    """Items from the window that no *earlier* brief of this edition has used (re-runs today are fine)."""
    since = (now_utc() - dt.timedelta(hours=hours)).isoformat()
    q = """SELECT id,source,title,url,published,summary,tags FROM items WHERE published >= ?
           AND id NOT IN (SELECT item_id FROM used WHERE edition = ? AND brief_date < ?)"""
    rows = db().execute(q + (" AND public_ok=1" if public else ""),
                        (since, edition(public), dt.date.today().isoformat())).fetchall()
    return _ranked(rows, min_rel)


def used_items(date: str, public: bool) -> list[dict]:
    """The items a brief was written from, for a redo: same news, no new fetch."""
    q = """SELECT id,source,title,url,published,summary,tags FROM items
           WHERE id IN (SELECT item_id FROM used WHERE edition = ? AND brief_date = ?)"""
    return _ranked(db().execute(q, (edition(public), date)).fetchall(), 0)


def _ranked(rows: list, min_rel: int) -> list[dict]:
    out = []
    for iid, source, title, url, pub, summary, tags in rows:
        t = json.loads(tags or "{}")
        if t.get("relevance", 0) >= min_rel:
            out.append({"id": iid, "source": source, "title": title, "url": url, "published": pub,
                        "summary": summary, **t})
    out.sort(key=lambda x: x["published"], reverse=True)          # newest first...
    return sorted(out, key=lambda x: -x.get("relevance", 0))       # ...within relevance


def write_brief(nodes: list[dict], hours: int, public: bool, use_llm: bool, context_days: int = 14,
                redo: str | None = None) -> Path:
    """Today's brief from new items, or with `redo` (a date) that day's brief again from the items it used."""
    today = redo or dt.date.today().isoformat()
    if redo:
        items = used_items(redo, public)
        if not items:
            raise SystemExit(f"No recorded items for the {edition(public)} brief of {redo}; nothing to redo from.")
        row = db().execute("SELECT data, hours FROM inputs WHERE brief_date=? AND edition=?",
                           (redo, edition(public))).fetchone()
        if row:
            data, hours = json.loads(row[0]), row[1]
        elif redo == dt.date.today().isoformat():
            data = eia_weekly() + eu_gas_storage()  # not saved for this brief; today's figures are the same
        else:
            data = []
            log(f"no saved data block for {redo}; redoing without one")
        log(f"redo {redo}: {len(items)} items, {len(data)} data lines, no new fetch")
    else:
        items = recent_items(hours, public)
        data = eia_weekly() + eu_gas_storage()
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

    if not items:
        raise SystemExit(f"No relevant items in the last {hours}h; not writing a brief. Check the feed log above.")
    items = items[:120]
    history = ledger_context(public, context_days, before=today)
    map_txt = "\n".join(f"- {n['label']} ({n['id']}): " + " | ".join(n["chain"]) for n in nodes)
    items_txt = "\n".join(json.dumps({k: i.get(k) for k in
                                      ("title", "url", "source", "published", "summary", "nodes", "why", "relevance")})
                          for i in items)
    user = prompt("brief_user", date=today,
                  edition="PUBLIC (free/primary sources only)" if public else "PRIVATE",
                  exposure_map=map_txt, context_days=context_days,
                  history=history or "(no earlier editions)",
                  titles=recent_titles(public, today) or "(none)", data="\n".join(data) or "(none today)",
                  hours=hours, count=len(items), items=items_txt)
    brief = llm(user, BRIEF_MODEL, 16000, prompt("brief_system"), effort="medium")
    path.write_text(brief)
    con = db()
    # A re-run replaces today's list, so only items the final brief saw count as used.
    con.execute("DELETE FROM used WHERE edition=? AND brief_date=?", (edition(public), today))
    con.executemany("INSERT OR REPLACE INTO used VALUES (?,?,?)",
                    [(i["id"], edition(public), today) for i in items])
    con.execute("INSERT OR REPLACE INTO inputs VALUES (?,?,?,?)", (today, edition(public), json.dumps(data), hours))
    con.commit()
    ledger_summary(today, public, brief)  # so tomorrow's run knows what today's said
    return path


# --------------------------------------------------------------------------- memory



def brief_files(public: bool) -> dict[str, Path]:
    """date -> brief file for this edition (ignores samples and other suffixes)."""
    pat = re.compile(r"(\d{4}-\d{2}-\d{2})" + ("-public" if public else "") + r"\.md")
    return {m.group(1): f for f in BRIEF_DIR.glob("*.md") if (m := pat.fullmatch(f.name))}


NOT_STORIES = {"also noted", "still developing", "watch next"}


def recent_titles(public: bool, before: str, n: int = 7) -> str:
    """The top two story headings of the last `n` briefs before `before`: what the episode titles showed."""
    out = []
    for date, f in sorted(brief_files(public).items()):
        if date < before:
            heads = [h.strip() for h in re.findall(r"^## (.+)$", f.read_text(), flags=re.M)
                     if h.strip().lower() not in NOT_STORIES]
            out.append(f"- {date}: " + " / ".join(heads[:2]))
    return "\n".join(out[-n:])


def ledger_summary(date: str, public: bool, brief: str) -> str:
    """Cached per brief; regenerated only if the brief file changed."""
    h = hashlib.sha1(brief.encode()).hexdigest()[:16]
    con = db()
    row = con.execute("SELECT brief_hash, summary FROM ledger WHERE brief_date=? AND edition=?",
                      (date, edition(public))).fetchone()
    if row and row[0] == h:
        return row[1]
    summary = llm(prompt("ledger", brief=brief), TAG_MODEL, 4000, effort="low").strip()
    con.execute("INSERT OR REPLACE INTO ledger VALUES (?,?,?,?)", (date, edition(public), h, summary))
    con.commit()
    return summary


def ledger_context(public: bool, days: int, before: str | None = None) -> str:
    """Ledger entries for this edition's briefs from the `days` days before `before` (default today)."""
    end = dt.date.fromisoformat(before) if before else dt.date.today()
    since = (end - dt.timedelta(days=days)).isoformat()
    out = []
    for date, f in sorted(brief_files(public).items()):
        if since <= date < end.isoformat():
            try:
                out.append(f"### {date}\n{ledger_summary(date, public, f.read_text())}")
            except Exception as e:  # noqa: BLE001  missing memory must not block today's brief
                log(f"ledger for {date} failed: {e}")
    return "\n\n".join(out)


# --------------------------------------------------------------------------- cli

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "ingest", "tag", "brief", "redo"])
    ap.add_argument("--public", action="store_true", help="only public_ok sources")
    ap.add_argument("--no-llm", action="store_true", help="keyword tagging, plain digest")
    ap.add_argument("--require-llm", action="store_true",
                    help="fail instead of falling back to --no-llm when a key is missing (for CI)")
    ap.add_argument("--hours", type=int, default=30)
    ap.add_argument("--context-days", type=int, default=14,
                    help="how many days of earlier briefs the writer is reminded of")
    ap.add_argument("--fixtures", type=Path, help="read saved feeds from this folder instead of the web")
    ap.add_argument("--date", help="redo: the brief to rewrite (YYYY-MM-DD, default today)")
    a = ap.parse_args()

    use_llm = not a.no_llm
    missing = {PROVIDER_KEYS[split_model(m)[0]] for m in (TAG_MODEL, BRIEF_MODEL) if not has_key(m)}
    if use_llm and missing:
        if a.require_llm:
            raise SystemExit(f"{', '.join(sorted(missing))} not set (--require-llm)")
        log(f"{', '.join(sorted(missing))} not set -> running with --no-llm")
        use_llm = False
    if use_llm:
        log(f"models: tag={TAG_MODEL} brief={BRIEF_MODEL}")

    sources = [s for s in load_toml("sources.toml")["source"] if s.get("public_ok") or not a.public]
    if os.environ.get("GITHUB_ACTIONS"):  # these sites block GitHub's servers
        sources = [s for s in sources if not s.get("local_only")]
    nodes = load_toml("exposure_map.toml")["node"]

    if a.cmd in ("run", "ingest"):
        log(f"ingested {ingest(sources, a.fixtures)} new items")
    if a.cmd in ("run", "tag"):
        log(f"tagged {tag(nodes, use_llm)} items")
    if a.cmd in ("run", "brief"):
        log(f"brief written to {write_brief(nodes, a.hours, a.public, use_llm, a.context_days)}")
    if a.cmd == "redo":
        date = a.date or dt.date.today().isoformat()
        log(f"brief rewritten: {write_brief(nodes, a.hours, a.public, use_llm, a.context_days, redo=date)}")


if __name__ == "__main__":
    main()
