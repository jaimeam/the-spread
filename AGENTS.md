# The Spread: notes for coding agents

A daily oil & gas / refining / petchem / midstream brief, turned into a podcast episode.
`pipeline.py` ingests feeds → tags items (Haiku) → writes the brief (Opus).
`podcast.py` writes the spoken script (Opus) → MP3 (ElevenLabs) → `site/feed.xml`.
`README.md` covers setup and usage. This file covers the rules.

## Hard rules

- **Never run the pipeline for real without the owner's explicit approval, each time.** That means
  `gh workflow run` (including dry runs), and local `pipeline.py` / `podcast.py` runs with
  API keys set. They spend Claude and ElevenLabs credits, and a non-dry run publishes an
  episode to Spotify. Approval for one run doesn't cover the next.
- Free to run without asking: offline tests (`--fixtures fixtures --no-llm`, `--script-only --no-llm`),
  stubbed-model tests in a scratch directory, `py_compile`, and read-only `gh run view` / `gh run list`.
- The repo is **public**. Never commit keys, `brief.db`, or private briefs (`briefs/YYYY-MM-DD.md`
  without `-public`). `.gitignore` covers the last two; keep it that way.
- **Don't break the feed for followers.** Never change the feed URL (`https://jaimeam.github.io/the-spread/feed.xml`),
  so don't rename the repo or account. Never change `slug` in `podcast.toml`: episode GUIDs are `<slug>-<date>`,
  and changing it re-lists every episode as new.
- Licensing guard: only `public_ok = true` sources reach the public edition (the one CI publishes).
  Google News stays `public_ok = false` unless the owner decides otherwise.

## How it runs

- GitHub Actions (`.github/workflows/daily.yml`), weekdays 05:00 UTC: public edition only.
  Monday looks back 78h, other days 30h. `dry_run` input = brief + script as an artifact, no audio, no publish.
- The bot commits to `main` every run (`briefs/`, `scripts/`, `site/`, `state/public.db`). Run `git pull` before working.
- CI state lives in `state/public.db` (`BRIEF_DB`). The local `brief.db` is separate and git-ignored.
- Removing or redoing episodes: `manage.yml` (Actions, manual) runs `podcast.py remove DATE` or re-makes
  script + audio from `briefs/DATE-public.md`, commits, then calls `publish.yml`. `publish.yml` also deploys
  on any push touching `site/`. A redone episode gets a new MP3 name (`DATE-<unix time>.mp3`) and keeps its GUID.
  A `redo` spends credits and publishes, so it needs approval like any run. `remove` publishes too, so ask before running it.
- Memory: the `used` table stops an item appearing in two briefs. The `ledger` table holds a short
  summary per brief, and the last 14 days of them are given to the brief writer (`--context-days`).
- Secrets on GitHub: `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`. Optional: `EIA_API_KEY`, `AGSI_API_KEY`.
- Local Python: macOS `python3` is 3.9. Use `/opt/homebrew/bin/python3` (needs 3.11+). Standard library only, no pip.

## Show format (owner's decisions)

- The voice is an ElevenLabs **clone of the owner's voice**. The spoken disclosure (`spoken_disclosure`)
  is said once, right after the date, and nowhere else in the audio. Show notes and the show description also disclose it.
- No "Signals I'm not sure about" section, and no uncertain-signal asides. The brief ends at "Watch next".
- Still keep fact vs claim, and "(unconfirmed - headline only)" marking.
- Numbers only from items or the data block, with a source tag. Paraphrase; quotes of at most 10 words.

## Gotchas

- Current Claude models always think, and thinking counts against `max_tokens`. Keep limits generous
  (brief/script 16k, tagging 8k). `llm()` raises on `max_tokens` / `refusal` rather than saving half an answer.
- Some sites block GitHub's servers but work locally: OIES (403), GDELT (429). Check a new feed both
  locally and in CI logs before relying on it. Most corporate and agency sites (OPEC, IEA, BP, Shell, FERC) block scripts.
- An episode is a full ElevenLabs request (~6-7k characters on a normal day). Credits are the binding cost;
  Claude is about $0.15-0.30 per run.

## Open items

- Episode length that follows the news (short on quiet days) or skipping thin days. Not decided yet.
- First CI run with the 14 new sources (added 2026-10-12) hasn't completed yet. The dry run was cancelled during tagging.
