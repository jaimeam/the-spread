#!/usr/bin/env python3
"""
Turn a written brief into a podcast episode and a podcast RSS feed.

  brief (markdown) -> spoken script (Claude) -> MP3 (ElevenLabs) -> site/feed.xml

Usage
  python podcast.py episode briefs/2026-10-12-public.md   # make today's episode + rebuild feed
  python podcast.py episode BRIEF --script-only           # write the script, skip audio
  python podcast.py feed                                  # rebuild feed.xml from site/episodes.json

Environment
  SCRIPT_MODEL           model for the script rewrite, "provider:model" (default: BRIEF_MODEL).
                         Needs that provider's key, unless you pass --no-llm (plain markdown cleanup;
                         fine for testing, not for a real episode).
  ELEVENLABS_API_KEY     elevenlabs.io -> profile -> API keys
  ELEVENLABS_VOICE_ID    the voice to use (Voice Library -> copy voice ID)
  ELEVENLABS_MODEL       default eleven_v4 (10,000 chars per request)
  PODCAST_BASE_URL       public URL where site/ is served, e.g. https://you.github.io/the-spread

Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
import re
import sys
import tomllib
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

import pipeline

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
EPISODES_DIR = SITE / "episodes"
INDEX = SITE / "episodes.json"
SCRIPTS = ROOT / "scripts"
MODEL = os.environ.get("ELEVENLABS_MODEL") or "eleven_v4"
CHUNK = 9000  # eleven_v4 takes 10,000 chars per request, so a normal episode is one call
BITRATE = 128_000  # mp3_44100_128


def cfg() -> dict:
    with open(ROOT / "podcast.toml", "rb") as f:
        c = tomllib.load(f)["podcast"]
    c["base_url"] = os.environ.get("PODCAST_BASE_URL", c.get("base_url", "")).rstrip("/")
    return c


# --------------------------------------------------------------------------- script

def markdown_to_plain(md: str) -> str:
    """Fallback when there's no API key: strip markdown so TTS doesn't read symbols aloud."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)          # links -> text
    text = re.sub(r"^Sources?:.*$", "", text, flags=re.M)         # drop source lines
    text = re.sub(r"\s*\[[^\]]+\]", "", text)                     # [EIA] tags
    text = re.sub(r"[#*_>`|]", "", text)
    text = re.sub(r"^\s*-\s+", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def write_script(brief: Path, c: dict, use_llm: bool) -> str:
    md = brief.read_text()
    if not use_llm:
        return markdown_to_plain(md)
    system = pipeline.prompt("script_system", intro=c["spoken_intro"], disclosure=c["spoken_disclosure"],
                             outro=c["spoken_outro"])
    return pipeline.llm(pipeline.prompt("script_user", brief=md), pipeline.SCRIPT_MODEL, 16000, system,
                        effort="medium").strip()


# --------------------------------------------------------------------------- audio

def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split on paragraph, then sentence boundaries."""
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        pieces = [para] if len(para) <= size else re.split(r"(?<=[.!?])\s+", para)
        for p in pieces:
            if len(cur) + len(p) + 2 > size and cur:
                out.append(cur.strip())
                cur = ""
            cur += p + "\n\n"
    if cur.strip():
        out.append(cur.strip())
    return out


def tts(text: str, prev: str = "", nxt: str = "") -> bytes:
    voice = os.environ["ELEVENLABS_VOICE_ID"]
    body = {"text": text, "model_id": MODEL}
    if prev:
        body["previous_text"] = prev[-1000:]
    if nxt:
        body["next_text"] = nxt[:1000]
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps(body).encode(),
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json",
                 "Accept": "audio/mpeg"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def synthesize(script: str, out: Path) -> None:
    parts = chunks(script)
    audio = b""
    for i, part in enumerate(parts):
        pipeline.log(f"TTS chunk {i + 1}/{len(parts)} ({len(part)} chars)")
        audio += tts(part, parts[i - 1] if i else "", parts[i + 1] if i + 1 < len(parts) else "")
    out.write_bytes(audio)  # MP3 frames concatenate cleanly at the same bitrate


# --------------------------------------------------------------------------- feed

def load_index() -> list[dict]:
    return json.loads(INDEX.read_text()) if INDEX.exists() else []


def build_feed(c: dict) -> Path:
    if not c["base_url"]:
        raise SystemExit("Set PODCAST_BASE_URL (or base_url in podcast.toml) before building the feed.")
    base = c["base_url"]
    items = []
    for ep in sorted(load_index(), key=lambda e: e["date"], reverse=True):
        mp3 = EPISODES_DIR / ep["file"]
        size = mp3.stat().st_size
        secs = int(size * 8 / BITRATE)
        pub = email.utils.format_datetime(
            dt.datetime.fromisoformat(ep["date"]).replace(hour=6, tzinfo=dt.timezone.utc))
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep['notes'])}</description>
      <content:encoded><![CDATA[{ep['notes_html']}]]></content:encoded>
      <enclosure url="{base}/episodes/{ep['file']}" length="{size}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(c['slug'])}-{ep['date']}</guid>
      <pubDate>{pub}</pubDate>
      <itunes:duration>{secs // 60}:{secs % 60:02d}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
      <itunes:episodeType>full</itunes:episodeType>
    </item>""")
    sub = f'<itunes:category text="{escape(c["subcategory"])}"/>' if c.get("subcategory") else ""
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(c['title'])}</title>
    <link>{base}/</link>
    <atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{escape(c['description'])}</description>
    <language>{c['language']}</language>
    <copyright>{escape(c['author'])}</copyright>
    <itunes:author>{escape(c['author'])}</itunes:author>
    <itunes:owner><itunes:name>{escape(c['author'])}</itunes:name><itunes:email>{escape(c['email'])}</itunes:email></itunes:owner>
    <itunes:image href="{base}/{c['cover']}"/>
    <itunes:category text="{escape(c['category'])}">{sub}</itunes:category>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
{chr(10).join(items)}
  </channel>
</rss>
"""
    out = SITE / "feed.xml"
    out.write_text(feed)
    return out


def notes_from_brief(md: str, c: dict) -> tuple[str, str]:
    """Show notes: the written brief, with sources, plus the AI-voice disclosure."""
    plain = markdown_to_plain(md)[:3500]
    links = re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", md)
    html_links = "".join(f'<li><a href="{escape(u)}">{escape(t)}</a></li>' for t, u in dict.fromkeys(links))
    html = (f"<p>{escape(c['disclosure'])}</p>"
            f"<p>{escape(plain[:1200])}...</p><p><strong>Sources</strong></p><ul>{html_links}</ul>")
    return f"{c['disclosure']}\n\n{plain}", html


# --------------------------------------------------------------------------- cli

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["episode", "feed"])
    ap.add_argument("brief", nargs="?", type=Path)
    ap.add_argument("--script-only", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--date", help="episode date (default: from brief filename, else today)")
    a = ap.parse_args()
    c = cfg()

    if a.cmd == "episode":
        if not a.brief:
            raise SystemExit("episode needs a brief file")
        m = re.match(r"\d{4}-\d{2}-\d{2}", a.brief.name)
        date = a.date or (m.group(0) if m else dt.date.today().isoformat())
        use_llm = not a.no_llm
        if use_llm and not pipeline.has_key(pipeline.SCRIPT_MODEL):
            key = pipeline.PROVIDER_KEYS[pipeline.split_model(pipeline.SCRIPT_MODEL)[0]]
            raise SystemExit(f"{key} not set for SCRIPT_MODEL={pipeline.SCRIPT_MODEL}; pass --no-llm to test without it")
        SCRIPTS.mkdir(exist_ok=True)
        script = write_script(a.brief, c, use_llm)
        (SCRIPTS / f"{date}.txt").write_text(script)
        pipeline.log(f"script: {len(script)} chars, ~{len(script.split()) / 150:.1f} min")
        if a.script_only:
            return
        EPISODES_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{date}.mp3"
        synthesize(script, EPISODES_DIR / fname)
        notes, notes_html = notes_from_brief(a.brief.read_text(), c)
        nice = dt.date.fromisoformat(date).strftime("%a %d %b %Y")
        idx = [e for e in load_index() if e["date"] != date]
        idx.append({"date": date, "file": fname, "title": f"{c['title']} - {nice}",
                    "notes": notes, "notes_html": notes_html})
        INDEX.write_text(json.dumps(idx, indent=1))
    pipeline.log(f"feed: {build_feed(c)}")


if __name__ == "__main__":
    main()
