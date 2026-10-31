# The Spread

A daily brief on oil & gas, refining, petchem and midstream: what moved, and
why. It ingests free and official sources, tags each item against
an exposure map, and has Claude write the brief.

[![Podcast downloads, from OP3](https://jaimeam.github.io/the-spread/stats.svg)](https://jaimeam.github.io/the-spread/stats.html)

**[Listener stats →](https://jaimeam.github.io/the-spread/stats.html)** downloads per day and per episode, apps and countries.

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

Monday's scheduled run looks back 78 hours to cover the weekend. For a private brief every morning, use cron
(`0 7 * * 1-5 cd /path && python pipeline.py run`) or a scheduled task.

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
- **Transcripts:** each MP3 has `DATE.txt` (the script) and `DATE.vtt` (timed captions, from
  ElevenLabs' character timings) beside it, linked in the feed with `<podcast:transcript>`.
  Apple Podcasts shows them if you pick your own transcripts in Podcasts Connect; Spotify ignores them.
  Episodes before 2026-10-19 have the text only.
- **Spotify:** the same feed, submitted through Spotify for Creators. New episodes appear
  there automatically after each run.
- **Other apps:** any podcast app that accepts a feed URL (Pocket Casts, Overcast, Apple Podcasts).

## Running it every morning

An always-on machine (a mini PC, a home server) runs every job and pushes the results to `main`;
GitHub then only deploys the site (*Publish site* runs on any push touching `site/`). GitHub's own
scheduler is best-effort (in October 2026 it delivered the morning runs about 5 hours late), so
it is kept only as a fallback for the daily episode.

| Job | When (UTC) | What |
|---|---|---|
| daily (`the-spread-trigger.timer`) | weekdays 04:41 | brief + episode + feed. Uses Claude + ElevenLabs credits, publishes to Spotify |
| ingest (`the-spread-ingest.timer`) | 00:23, 08:23, 12:23, 16:23, 20:23 | fetches every public source into `state/public.db`. Some feeds keep only 10-15 items and would otherwise roll over before the next morning. No keys, publishes nothing |
| stats (`the-spread-stats.timer`) | 10:37, 18:37 | [Listener stats](#listener-stats) from OP3. No credits |

The daily job skips weekends and a day that's already published, and won't start after 05:15 UTC
(`--force` overrides). GitHub's fallback (`daily.yml`) runs at 05:47 UTC and does nothing if the
episode already exists, so the two never overlap. Monday's run looks back 78 hours to cover the weekend.

### Setting up the machine

Any systemd Linux with Python 3.11+, git and flock (util-linux).

1. GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token.
   Repository access: only `the-spread`. Permissions: **Contents: Read and write**. Note the expiry date.
   The jobs push with it, and that push is what starts *Publish site* (pushes made with a
   workflow's own `GITHUB_TOKEN` don't start other workflows).
2. `git clone https://github.com/jaimeam/the-spread`, then `cd the-spread/trigger && sudo ./install.sh`.
   The first run writes `/etc/the-spread/env` (from `env.example`, root only) and stops: fill in the keys
   (`sudo nano /etc/the-spread/env`), including `OP3_TOKEN`, then run `sudo ./install.sh` again and paste
   the token. It installs `the-spread-run` and the units, checks Python, the clone and that the token
   may push (in the same sandbox the jobs use, pushing nothing), then enables the timers.
3. Logs: `journalctl -u the-spread-trigger -u the-spread-ingest -u the-spread-stats`. Next runs:
   `systemctl list-timers 'the-spread-*'`. Run a job now: `sudo systemctl start the-spread-ingest`
   (or `-stats`; `-trigger` is the daily episode, which spends credits). New token:
   `sudo ./install.sh --new-token`. After changing `trigger/`: `git pull`, `sudo ./install.sh`.

The jobs run as a sandboxed system user (`DynamicUser`) in their own clone at `/var/lib/the-spread/repo`,
reset to `origin/main` before every job, so never edit files there. One job runs at a time. If `main`
moved during a job (e.g. a *Manage episodes* run), the job rebases its commit before pushing.
The timers wake the machine from suspend, and run at boot if the machine was off at their time.
Feed problems (dead, empty, or a gap where a feed rolled over unread) show as warnings in the ingest log.
Sources marked `local_only` (blocked from GitHub's servers) are fetched here, since `GITHUB_ACTIONS` isn't set.

### GitHub as fallback

1. GitHub Pages on a private repo needs a paid plan; on the free plan, make the repo public. That's fine
   because only public-edition sources are ever used.
2. Keep the secrets listed at the top of `.github/workflows/daily.yml`: the 05:47 fallback and
   *Manage episodes* use them. The run fails, instead of publishing a keyword digest, if the model keys are missing.
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → The Spread daily → **Run workflow** to run it by hand. Tick **Dry run** to get just the
   brief and script (downloadable under the run's Artifacts) with no audio cost and nothing published.
   *Ingest feeds* and *Listener stats* can also be run by hand from the Actions tab.
5. Add `https://<you>.github.io/<repo>/feed.xml` to your podcast app (Pocket Casts,
   Overcast and Apple Podcasts all accept a feed URL), and submit it in Spotify for Creators to go public.

### Listener stats

Episode MP3 links (from `op3_from` in `podcast.toml` on) go through [OP3](https://op3.dev), which counts
downloads, excludes bots, and updates once a day. The machine's stats job runs `stats.py`, which reads
those counts and publishes [stats.html](https://jaimeam.github.io/the-spread/stats.html), the `stats.svg`
card shown at the top of this README, and `stats.json`. No credits used. OP3's own page for the show:
https://op3.dev/show/e0804b8a-fcbb-5fd2-b9bf-53881813334f. Until OP3 has found the show (through
Podcast Index, after a few days of downloads), the page and card say there's no data yet.

One-time setup: sign in at https://op3.dev/api/keys, create an API key, copy its **bearer token**, and put it
in `/etc/the-spread/env` as `OP3_TOKEN`. To use the GitHub workflow by hand too, add it as the repository
secret `OP3_TOKEN`. `python stats.py render` rebuilds the page from `site/stats.json` without calling OP3.

### Removing or redoing an episode

Actions → **Manage episodes** → Run workflow, then pick:

- **remove**: takes the episode out of the feed and deletes its MP3 and transcripts (no credits). The brief and
  script stay in the repo, so the brief-writer still remembers that day.
- **redo**: rewrites that day's brief from the same news items it used (nothing is fetched again),
  then writes a new script and re-records the audio, e.g. after you change the prompts.
  Uses Claude + ElevenLabs credits.
- **redo-script**: keeps the brief; writes a new script and re-records the audio, e.g. after you
  change the voice. Uses Claude + ElevenLabs credits.

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
| `stats.py` | Listener stats from OP3 -> `site/stats.html`, `stats.svg`, `stats.json` |
| `pipeline.py` | ingest -> SQLite -> tag (Claude Haiku) -> brief (Claude Opus) |
| `podcast.py`, `podcast.toml` | brief -> script -> MP3 -> podcast feed; show settings |
| `site/` | What GitHub Pages serves: `cover.png`, `feed.xml`, `episodes/` |
| `trigger/` | `the-spread-run` (runs a job and pushes), its systemd units and timers, `install.sh`, `env.example` |
| `.github/workflows/daily.yml` | The 05:47 UTC weekday fallback for the daily episode |
| `.github/workflows/ingest.yml`, `stats.yml` | Ingest feeds and Listener stats, by hand only (the machine runs them) |
| `.github/workflows/manage.yml`, `publish.yml` | Remove/redo an episode; republish the site (runs on every push touching `site/`) |
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
