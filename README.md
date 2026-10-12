# The Spread

A daily brief on oil & gas, refining, petchem and midstream: what moved, and
why. It ingests free and official sources, tags each item against
an exposure map, and has Claude write the brief.

## Run it

Needs Python 3.11+ and nothing else (standard library only).

```bash
export ANTHROPIC_API_KEY=...        # required for the AI steps
export EIA_API_KEY=...              # optional, free: eia.gov/opendata/register.php
export AGSI_API_KEY=...             # optional, free: agsi.gie.eu (EU gas storage)

python pipeline.py run              # private brief -> briefs/YYYY-MM-DD.md
python pipeline.py run --public     # public edition: only public_ok sources
python pipeline.py run --no-llm     # no key needed: keyword digest
python pipeline.py run --no-llm --fixtures fixtures --hours 999   # offline smoke test
```

To run it every morning, use cron (`0 7 * * 1-5 cd /path && python pipeline.py run`)
or a scheduled task.

## Choosing models

Each AI task has its own setting, written as `provider:model`:

| Setting | Task | Default |
|---|---|---|
| `TAG_MODEL` | Tags each news item (many small calls) | `anthropic:claude-haiku-5-5` |
| `BRIEF_MODEL` | Writes the brief (quality matters most) | `anthropic:claude-opus-5-5` |
| `SCRIPT_MODEL` | Rewrites the brief for audio | same as `BRIEF_MODEL` |

Providers: `anthropic` (needs `ANTHROPIC_API_KEY`) and `mistral` (needs `MISTRAL_API_KEY`).

On GitHub: Settings → Secrets and variables → Actions → **Variables** tab → New
repository variable, e.g. `TAG_MODEL` = `mistral:mistral-small-latest`. Delete the
variable to go back to the default. Locally: `export TAG_MODEL=mistral:mistral-small-latest`.

## Podcast

`podcast.py` turns a brief into an episode:
brief → spoken script (Claude) → MP3 (ElevenLabs) → `site/feed.xml`.

```bash
export ELEVENLABS_API_KEY=...
export ELEVENLABS_VOICE_ID=...       # pick a voice in ElevenLabs' Voice Library, copy its ID
export PODCAST_BASE_URL=https://<you>.github.io/<repo>

python podcast.py episode briefs/2026-10-12-public.md                # episode + feed
python podcast.py episode briefs/2026-10-12-public.md --script-only  # just the script, no audio cost
```

Edit `podcast.toml` before going live: the title, and above all `email` (Spotify and
Apple send ownership verification there, and it is visible in the feed).

## Running it every morning (GitHub Actions)

1. Create a GitHub repo and push this folder to it. GitHub Pages on a private repo needs a
   paid GitHub plan; on the free plan, make the repo public. That's fine because the workflow
   only ever uses public-edition sources.
2. Add the secrets listed at the top of `.github/workflows/daily.yml`.
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → The Spread daily → **Run workflow** once to test.
5. Add `https://<you>.github.io/<repo>/feed.xml` to your podcast app (Pocket Casts,
   Overcast and Apple Podcasts all accept a feed URL).
6. When you're ready to go public, submit the same feed URL in Spotify for Creators.
   New episodes then appear on Spotify automatically.

Until step 6 the feed isn't listed anywhere, but anyone with the URL can find it.

## Files

| File | What it is |
|---|---|
| `sources.toml` | Every feed, with `public_ok` marking what may appear in a public edition |
| `exposure_map.toml` | Chokepoints, assets and policies, each with its transmission chain. **This is your edge; refine it.** |
| `pipeline.py` | ingest -> SQLite -> tag (Claude Haiku) -> brief (Claude Opus) |
| `podcast.py`, `podcast.toml` | brief -> script -> MP3 -> podcast feed; show settings |
| `site/` | What GitHub Pages serves: `cover.png`, `feed.xml`, `episodes/` |
| `.github/workflows/daily.yml` | The weekday schedule |
| `brief.db` | Every item seen, with tags (created on first run) |
| `briefs/` | Output |

## Design choices

- **Licensing guard.** Items from `public_ok = false` sources (Google News
  headlines of paywalled outlets, your own subscriptions) never reach `--public`.
- **Numbers are sourced.** The brief prompt forbids figures that aren't in the
  items or the data block, and requires a source tag after each one.
- **Claims vs facts.** One-sided announcements are flagged as such.
- **Dead feeds don't break runs.** Each source fails independently.

## Next steps

1. Add the sources you trust: company IR pages, your subscriptions (private),
   regional regulators, Telegram/X analysts you rate.
2. Add more data series: CFTC positioning, Baker Hughes rigs, ENTSOG flows.
3. Track the exposure map's hit rate: which chains actually showed up in prices
   a week later. That's what turns this into analysis people pay for.
