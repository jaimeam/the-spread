# The Spread

A daily brief on oil & gas, refining, petchem and midstream: what moved, and
why. It ingests free and official sources, tags each item against
an exposure map, and has Claude write the brief.

## Run it

Needs Python 3.11+ and nothing else (standard library only). macOS ships 3.9 as `python3`;
use Homebrew's (`brew install python`, then `/opt/homebrew/bin/python3`).

```bash
export ANTHROPIC_API_KEY=...        # required for the AI steps
export EIA_API_KEY=...              # optional, free: eia.gov/opendata/register.php
export AGSI_API_KEY=...             # optional, free: agsi.gie.eu (EU gas storage)

python pipeline.py run              # private brief -> briefs/YYYY-MM-DD.md
python pipeline.py run --public     # public edition: only public_ok sources
python pipeline.py run --no-llm     # no key needed: keyword digest
python pipeline.py run --no-llm --fixtures fixtures --hours 999   # offline smoke test
python pipeline.py run --public --require-llm   # what CI runs: fails rather than falling back
```

Monday's CI run looks back 78 hours to cover the weekend. To run it locally every morning, use cron (`0 7 * * 1-5 cd /path && python pipeline.py run`)
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

### Using your own voice

1. ElevenLabs → Voices → Add a new voice → **Instant Voice Clone** (1-2 min of clean audio) or
   **Professional Voice Clone** (Creator plan or above, 30+ min of audio, verified with a live
   recording, trains in a few hours; noticeably closer to you).
2. Record yourself reading something like a brief, in your normal "radio" tone: a quiet room,
   no music, consistent distance from the mic, MP3 at 192 kbps or higher.
3. When it's ready, copy the voice ID (Voices → your voice → ID) into `ELEVENLABS_VOICE_ID`.
4. Change `description`, `disclosure` and `spoken_disclosure` in `podcast.toml` to say it's an AI
   clone of your voice (the spoken one is said once, right after the date).

Edit `podcast.toml` before going live: the title, and above all `email` (Spotify and
Apple send ownership verification there, and it is visible in the feed).

## Where it's published

- **Feed (the source of truth):** https://jaimeam.github.io/the-spread/feed.xml, served by GitHub
  Pages from `site/`. Episodes (MP3) and `cover.png` live there too. Never change this URL.
- **Spotify:** the same feed, submitted through Spotify for Creators. New episodes appear
  there automatically after each run.
- **Other apps:** any podcast app that accepts a feed URL (Pocket Casts, Overcast, Apple Podcasts).

## Running it every morning (GitHub Actions)

1. Create a GitHub repo and push this folder to it. GitHub Pages on a private repo needs a
   paid GitHub plan; on the free plan, make the repo public. That's fine because the workflow
   only ever uses public-edition sources.
2. Add the secrets listed at the top of `.github/workflows/daily.yml`. The run fails, instead of
   publishing a keyword digest, if the model keys are missing.
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → The Spread daily → **Run workflow** once to test. Tick **Dry run** to get just the
   brief and script (downloadable under the run's Artifacts) with no audio cost and nothing published.
5. Add `https://<you>.github.io/<repo>/feed.xml` to your podcast app (Pocket Casts,
   Overcast and Apple Podcasts all accept a feed URL).
6. When you're ready to go public, submit the same feed URL in Spotify for Creators.
   New episodes then appear on Spotify automatically.

Until step 6 the feed isn't listed anywhere, but anyone with the URL can find it.

### Removing or redoing an episode

Actions → **Manage episodes** → Run workflow, then pick:

- **remove**: takes the episode out of the feed and deletes its MP3 (no credits). The brief and
  script stay in the repo, so the brief-writer still remembers that day.
- **redo**: writes a new script and re-records the audio from that day's brief, e.g. after you
  change the voice. Uses Claude + ElevenLabs credits. To rewrite today's brief too, run *The Spread daily* again.

The site republishes within a couple of minutes. Podcast apps and Spotify update the next time they check
the feed (usually hours), and anyone who already downloaded the episode keeps their copy.
Old files remain in the public git history.

To fix only an episode's title and show notes: `python podcast.py notes 2026-10-12`, commit, push.

Locally: `python podcast.py remove 2026-10-12`, commit, push. Pushing changes under `site/` republishes
automatically (the *Publish site* workflow).

## Files

| File | What it is |
|---|---|
| `sources.toml` | Every feed, with `public_ok` marking what may appear in a public edition |
| `exposure_map.toml` | Chokepoints, assets and policies, each with its transmission chain. **This is your edge; refine it.** |
| `pipeline.py` | ingest -> SQLite -> tag (Claude Haiku) -> brief (Claude Opus) |
| `podcast.py`, `podcast.toml` | brief -> script -> MP3 -> podcast feed; show settings |
| `site/` | What GitHub Pages serves: `cover.png`, `feed.xml`, `episodes/` |
| `.github/workflows/daily.yml` | The weekday schedule |
| `.github/workflows/manage.yml`, `publish.yml` | Remove/redo an episode; republish the site |
| `brief.db` | Every item seen, with tags (local, git-ignored). CI keeps its public-only copy in `state/public.db` |
| `briefs/` | Output. Only `*-public.md` is committed; private briefs stay local |

## Design choices

- **Licensing guard.** Items from `public_ok = false` sources (Google News
  headlines of paywalled outlets, your own subscriptions) never reach `--public`.
- **Prompts live in `prompts/`.** One Markdown file per LLM call (tagging, brief, ledger,
  podcast script); `{placeholders}` are filled in by the code.
- **Numbers are sourced.** The brief prompt (`prompts/brief_system.md`) forbids figures that aren't in the
  items or the data block, and requires a source tag after each one.
- **Claims vs facts.** One-sided announcements are flagged as such.
- **Dead feeds don't break runs.** Each source fails independently.
- **No repeats.** An item used in one brief never reaches a later brief of the same edition.
  After each brief, the tag model writes a short ledger entry (stories, status, key figures,
  watch items). The next brief is given the last 14 days of entries (`--context-days`), so
  it reports only what's new on continuing stories and follows up on earlier "Watch next" items.

## Next steps

1. Add the sources you trust: company IR pages, your subscriptions (private),
   regional regulators, Telegram/X analysts you rate.
2. Add more data series: CFTC positioning, Baker Hughes rigs, ENTSOG flows.
3. Track the exposure map's hit rate: which chains actually showed up in prices
   a week later. That's what turns this into analysis people pay for.
