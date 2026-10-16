#!/usr/bin/env python3
"""
Turn a written brief into a podcast episode and a podcast RSS feed.

  brief (markdown) -> spoken script (Claude) -> MP3 (ElevenLabs) -> site/feed.xml

Usage
  python podcast.py episode briefs/2026-10-12-public.md   # make today's episode + rebuild feed
  python podcast.py episode BRIEF --script-only           # write the script, skip audio
  python podcast.py feed                                  # rebuild feed.xml from site/episodes.json
  python podcast.py remove 2026-10-12                     # drop an episode from the feed, delete its MP3
  python podcast.py notes 2026-10-12                      # rebuild an episode's title + show notes (no audio)

Each episode's MP3 gets a transcript beside it: DATE.txt (the script) and DATE.vtt (timed captions from
ElevenLabs' character timings). The feed links both with <podcast:transcript>.

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
import base64
import datetime as dt
import email.utils
import math
import json
import os
import re
import sys
import time
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


def tts(text: str, prev: str = "", nxt: str = "") -> tuple[bytes, dict | None]:
    """Audio for one chunk, plus ElevenLabs' per-character timings (None if they're missing)."""
    voice = os.environ["ELEVENLABS_VOICE_ID"]
    body = {"text": text, "model_id": MODEL}
    if prev:
        body["previous_text"] = prev[-1000:]
    if nxt:
        body["next_text"] = nxt[:1000]
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128",
        data=json.dumps(body).encode(),
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)
    return base64.b64decode(data["audio_base64"]), data.get("alignment")


def synthesize(script: str, out: Path) -> None:
    """Write the MP3, the script as DATE.txt and, when the timings are usable, captions as DATE.vtt."""
    parts = chunks(script)
    audio, timed = b"", []
    for i, part in enumerate(parts):
        pipeline.log(f"TTS chunk {i + 1}/{len(parts)} ({len(part)} chars)")
        clip, align = tts(part, parts[i - 1] if i else "", parts[i + 1] if i + 1 < len(parts) else "")
        offset = mp3_seconds(audio)  # each chunk's timings start at zero
        try:
            timed += [(ch, offset + a, offset + b) for ch, a, b in zip(
                align["characters"], align["character_start_times_seconds"], align["character_end_times_seconds"])]
            timed.append(("\n", 0, 0))  # chunks split at paragraphs
        except (TypeError, KeyError):
            timed = None  # partial captions would drift, so none at all
        audio += clip
    out.write_bytes(audio)  # MP3 frames concatenate cleanly at the same bitrate
    out.with_suffix(".txt").write_text(script.strip() + "\n")
    # Captions are a bonus: if the timings are missing or odd, the episode still goes out without them.
    try:
        out.with_suffix(".vtt").write_text(captions(timed))
    except Exception as e:  # noqa: BLE001
        out.with_suffix(".vtt").unlink(missing_ok=True)
        pipeline.log(f"no captions this episode: {e!r}")


def stamp(secs: float) -> str:
    ms = round(secs * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def captions(timed: list[tuple[str, float, float]] | None, width: int = 42) -> str:
    """WebVTT from per-character timings: cues of up to two lines, split at sentences and pauses."""
    if not timed:
        raise ValueError("no timings")
    words, cur, breaks = [], None, set()  # word: [text, start, end]
    for ch, a, b in timed:
        if ch.isspace():
            if ch == "\n" and words:
                breaks.add(len(words) - 1)
            cur = None
        elif cur is None:
            cur = [ch, a, b]
            words.append(cur)
        else:
            cur[0] += ch
            cur[2] = b
    cues, line = [], []
    for i, w in enumerate(words):
        line.append(w)
        n = len(" ".join(x[0] for x in line))
        end = w[0][-1]
        if (i in breaks or i == len(words) - 1 or n >= 2 * width - 16
                or (end in ".?!" and n >= 25) or (end in ",;:" and n >= width)
                or w[2] - line[0][1] >= 6):
            cues.append(line)
            line = []
    out = ["WEBVTT", ""]
    for n, cue in enumerate(cues, 1):
        ws = [x[0] for x in cue]
        lines = [" ".join(ws)]
        if len(lines[0]) > width:  # two lines, split where they come out most even
            k = min(range(1, len(ws)), key=lambda k: max(len(" ".join(ws[:k])), len(" ".join(ws[k:]))))
            lines = [" ".join(ws[:k]), " ".join(ws[k:])]
        start = cue[0][1]
        stop = max(cue[-1][2], start + 0.5)
        if n < len(cues):  # never overlap the next cue
            stop = min(stop, cues[n][0][1])
        out += [str(n), f"{stamp(start)} --> {stamp(stop)}", *lines, ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- feed

def mp3_seconds(mp3: Path | bytes) -> float:
    """Playing time, counted from the MPEG frame headers (the file size over-counts the ID3 tag)."""
    audio = mp3 if isinstance(mp3, bytes) else mp3.read_bytes()
    if not audio:
        return 0.0
    i = 10 + int.from_bytes(bytes(b & 0x7F for b in audio[6:10]), "big") if audio[:3] == b"ID3" else 0
    secs = 0.0
    while i + 4 <= len(audio) and audio[i] == 0xFF and audio[i + 1] & 0xFE == 0xFA:  # MPEG-1 Layer III
        kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)[audio[i + 2] >> 4]
        rate = (44100, 48000, 32000, 0)[(audio[i + 2] >> 2) & 3]
        if not kbps or not rate:
            break
        secs += 1152 / rate
        i += 144_000 * kbps // rate + ((audio[i + 2] >> 1) & 1)
    return secs or len(audio) * 8 / BITRATE


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
        # Round up: some players stop at the stated duration, and rounding down cut the last words.
        secs = math.ceil(mp3_seconds(mp3))
        pub = email.utils.format_datetime(
            dt.datetime.fromisoformat(ep["date"]).replace(hour=6, tzinfo=dt.timezone.utc))
        transcripts = "".join(
            f'\n      <podcast:transcript url="{base}/episodes/{mp3.stem}{ext}" type="{kind}" language="{c["language"]}"/>'
            for ext, kind in ((".vtt", "text/vtt"), (".txt", "text/plain")) if mp3.with_suffix(ext).exists())
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep['notes'])}</description>
      <content:encoded><![CDATA[{ep['notes_html']}]]></content:encoded>
      <enclosure url="{base}/episodes/{ep['file']}" length="{size}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(c['slug'])}-{ep['date']}</guid>
      <pubDate>{pub}</pubDate>
      <itunes:duration>{secs // 60}:{secs % 60:02d}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
      <itunes:episodeType>full</itunes:episodeType>{transcripts}
    </item>""")
    sub = f'<itunes:category text="{escape(c["subcategory"])}"/>' if c.get("subcategory") else ""
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
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


NOT_STORIES = {"also noted", "still developing", "watch next"}


def brief_sections(md: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """(heading, [(link text, url)]) for each ## section. Takes markdown links and bare URLs."""
    out = []
    for block in re.split(r"^## ", md, flags=re.M)[1:]:
        heading, _, body = block.partition("\n")
        links = re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", body)
        bare = re.sub(r"\[[^\]]+\]\(https?://[^)\s]+\)", "", body)
        links += [(re.sub(r"^www\.", "", u.split("/")[2]), u) for u in re.findall(r"https?://[^\s)<>]+", bare)]
        out.append((heading.strip(), list(dict.fromkeys(links))))
    return out


def episode_title(md: str, date: str, c: dict) -> str:
    """The day's top story headings, e.g. 'Hormuz: six ships hit in one day; Houthis strike Riyadh | 13 Oct'."""
    stories = [h for h, _ in brief_sections(md) if h.lower() not in NOT_STORIES]
    when = dt.date.fromisoformat(date).strftime("%-d %b")
    if not stories:
        return f"{c['title']} - {when}"
    head = stories[0]
    if len(stories) > 1 and len(head) + len(stories[1]) <= 75:
        head += "; " + stories[1]
    return f"{head} | {when}"


def lede(md: str) -> str:
    """The brief's opening sentences, between the title and the first story."""
    m = re.search(r"^# [^\n]*\n+(.*?)(?=^## |\Z)", md, re.M | re.S)
    text = re.sub(r"^\s*\*\*[^*\n]{1,30}:\*\*", "", m.group(1)) if m else ""  # a "**Top line:**" label
    text = re.sub(r"\s*\[[^\]]+\](\([^)]*\))?", "", text)
    return re.sub(r"[*_]", "", " ".join(text.split()))


def notes_from_brief(md: str, date: str, c: dict) -> tuple[str, str]:
    """Show notes: the brief's lede, the disclosure, the stories, a link to the written brief, and every source."""
    sections = brief_sections(md)
    stories = [h for h, _ in sections if h.lower() not in NOT_STORIES]
    brief_url = f"{c['repo_url']}/blob/main/briefs/{date}-public.md" if c.get("repo_url") else ""
    top = lede(md)

    plain = [p for p in (top, c["disclosure"]) if p]
    if stories:
        plain.append("In this episode:\n" + "\n".join(f"- {h}" for h in stories))
    if brief_url:
        plain.append(f"Full written brief: {brief_url}")
    src = "\n".join(f"- {t}: {u}" for _, links in sections for t, u in links)
    text = "\n\n".join(plain)
    if src:  # podcast apps cut descriptions at 4,000 characters
        room = 3900 - len(text)
        if len(src) > room:
            src = src[:src.rfind("\n", 0, room - 60)] + "\n- More in the full written brief."
        text += "\n\nSources:\n" + src

    html = "".join(f"<p>{escape(p)}</p>" for p in (top, c["disclosure"]) if p)
    if stories:
        html += "<p><strong>In this episode</strong></p><ul>" + "".join(f"<li>{escape(h)}</li>" for h in stories) + "</ul>"
    if brief_url:
        html += f'<p><a href="{escape(brief_url)}">Read the full written brief</a></p>'
    groups = [(h, links) for h, links in sections if links]
    if groups:
        html += "<p><strong>Sources</strong></p>"
        for h, links in groups:
            html += (f"<p>{escape(h)}</p><ul>"
                     + "".join(f'<li><a href="{escape(u)}">{escape(t)}</a></li>' for t, u in links) + "</ul>")
    return text, html


def episode_meta(brief: Path, date: str, c: dict) -> dict:
    md = brief.read_text()
    notes, notes_html = notes_from_brief(md, date, c)
    return {"title": episode_title(md, date, c), "notes": notes, "notes_html": notes_html}


def refresh_notes(date: str, c: dict) -> None:
    """Rebuild an existing episode's title and show notes from its brief. Audio unchanged."""
    idx = load_index()
    eps = [e for e in idx if e["date"] == date]
    if not eps:
        raise SystemExit(f"No episode dated '{date}'.")
    for e in eps:
        e.update(episode_meta(ROOT / "briefs" / f"{date}-public.md", date, c))
    INDEX.write_text(json.dumps(idx, indent=1))
    pipeline.log(f"notes refreshed for {date}: {eps[0]['title']}")


def drop_files(mp3_name: str) -> None:
    """Delete an episode's MP3 and its transcripts."""
    mp3 = EPISODES_DIR / mp3_name
    for f in (mp3, mp3.with_suffix(".txt"), mp3.with_suffix(".vtt")):
        f.unlink(missing_ok=True)


def remove_episode(date: str) -> None:
    """Take an episode out of the feed and delete its MP3. Its brief and script stay as records."""
    idx = load_index()
    gone = [e for e in idx if e["date"] == date]
    if not gone:
        raise SystemExit(f"No episode dated '{date}'. Episodes: {', '.join(e['date'] for e in idx) or 'none'}")
    for e in gone:
        drop_files(e["file"])
    INDEX.write_text(json.dumps([e for e in idx if e["date"] != date], indent=1))
    pipeline.log(f"removed episode {date}")


# --------------------------------------------------------------------------- cli

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["episode", "feed", "remove", "notes"])
    ap.add_argument("brief", nargs="?", help="episode: the brief file. remove, notes: the episode date (YYYY-MM-DD)")
    ap.add_argument("--script-only", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--date", help="episode date (default: from brief filename, else today)")
    a = ap.parse_args()
    c = cfg()

    if a.cmd == "remove":
        remove_episode(a.brief or a.date or "")
    if a.cmd == "notes":
        refresh_notes(a.brief or a.date or "", c)
    if a.cmd == "episode":
        if not a.brief:
            raise SystemExit("episode needs a brief file")
        a.brief = Path(a.brief)
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
        old = [e for e in load_index() if e["date"] == date]
        # A redone episode gets a new file name, so apps fetch the new audio instead of a cached copy.
        fname = f"{date}-{int(time.time())}.mp3" if old else f"{date}.mp3"
        synthesize(script, EPISODES_DIR / fname)
        for e in old:
            drop_files(e["file"])
        idx = [e for e in load_index() if e["date"] != date]
        idx.append({"date": date, "file": fname, **episode_meta(a.brief, date, c)})
        INDEX.write_text(json.dumps(idx, indent=1))
    pipeline.log(f"feed: {build_feed(c)}")


if __name__ == "__main__":
    main()
