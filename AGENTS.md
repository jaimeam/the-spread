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
- **The show is live on Spotify** (submitted 2026-10-12), so the feed is public and followed.
  Prefer getting an episode right before 05:00 UTC over redoing it later: Spotify may keep stale audio.
- **Don't break the feed for followers.** Never change the feed URL (`https://jaimeam.github.io/the-spread/feed.xml`),
  so don't rename the repo or account. Never change `slug` in `podcast.toml`: episode GUIDs are `<slug>-<date>`,
  and changing it re-lists every episode as new.
  Never change `guid` in `podcast.toml` either (the show's Podcast Index ID, used by OP3 stats).
- Download stats: MP3 links for episodes dated from `op3_from` on go through `op3.dev` (public stats, then a
  redirect to GitHub Pages). Don't move older episodes onto OP3: changing their link can make Spotify re-fetch them.
- Licensing guard: only `public_ok = true` sources reach the public edition (the one CI publishes).
  Google News stays `public_ok = false` unless the owner decides otherwise.

## How it runs

- GitHub Actions (`.github/workflows/daily.yml`), weekdays 04:41 UTC, with a 05:17 UTC backup that skips
  if `briefs/DATE-public.md` already exists (GitHub drops or delays some scheduled runs, by ~5h in Oct 2026).
  The on-time start comes from the owner's mini PC: a systemd timer (`trigger/`) dispatches the workflow at
  04:41 UTC with `skip_if_done=true`; the GitHub schedule is the fallback. Public edition only.
  Monday looks back 78h, other days 30h. `dry_run` input = brief + script as an artifact, no audio, no publish.
- The bot commits to `main` every run (`briefs/`, `scripts/`, `site/`, `state/public.db`). Run `git pull` before working.
- CI state lives in `state/public.db` (`BRIEF_DB`). The local `brief.db` is separate and git-ignored.
- Removing or redoing episodes: `manage.yml` (Actions, manual) runs `podcast.py remove DATE`, or `redo`
  (`pipeline.py redo --date DATE` rewrites the brief from the items in the `used` table, no fetch, then
  script + audio), or `redo-script` (script + audio from the existing brief). It commits, then calls `publish.yml`.
  The data block and look-back of each brief are saved in the `inputs` table so a redo uses the same. `publish.yml` also deploys
  on any push touching `site/`. A redone episode gets a new MP3 name (`DATE-<unix time>.mp3`) and keeps its GUID.
  A `redo` spends credits and publishes, so it needs approval like any run. `remove` publishes too, so ask before running it.
- Memory: the `used` table stops an item appearing in two briefs. The `ledger` table holds a short
  summary per brief, and the last 14 days of them are given to the brief writer (`--context-days`).
- Secrets on GitHub: `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`. Optional: `EIA_API_KEY`, `AGSI_API_KEY`.
- Local Python: macOS `python3` is 3.9. Use `/opt/homebrew/bin/python3` (needs 3.11+). Standard library only, no pip.

## Show format (owner's decisions)

- Every LLM prompt lives in `prompts/` (one Markdown file per call), loaded by `pipeline.prompt()`.
- Length follows the news: 2-7 minutes, never padded (`prompts/script_system.md`).
- "What to watch" is said once, in the closing section. Each story makes clear who is hit, in its own words (no "Who feels it:" label or other repeated formulas), and holds no watch items.
- Comparisons with earlier figures only when they matter, said flatly ("yesterday X, today Y"). Never "we reported" / "we gave you".
- The voice is an ElevenLabs **clone of the owner's voice**. The spoken disclosure (`spoken_disclosure`)
  is said once, right after the date, and nowhere else in the audio. Show notes and the show description also disclose it.
- Fixed running order: intro, date and disclosure → headline → "On the markets" → stories → quick hits →
  "What to watch next" → outro. Stories are linked by topic, not numbered.
- A revised count for the same event is a revision, not a trend. Anything older than a day is dated.
- Episode title = the brief's top one or two `##` headlines + date. Titles must not look alike day to day: the brief writer
  sees the last 7 editions' top headlines and leads with the new fact, not a running-story label like "Hormuz:". Show notes = the brief's lede, the
  disclosure, the story list, a link to the written brief on GitHub, and every source.
  `podcast.py notes DATE` rebuilds them for an existing episode (no audio; publishes when pushed).
- No "Signals I'm not sure about" section, and no uncertain-signal asides. The brief ends at "Watch next".
- Still keep fact vs claim, and "(unconfirmed - headline only)" marking.
- Numbers only from items or the data block, with a source tag. Paraphrase; quotes of at most 10 words.
- Every episode ships a transcript (`.txt`) and captions (`.vtt`) next to its MP3. Captions are best-effort:
  if ElevenLabs' timings are missing, the episode still publishes with the text transcript only.

## Gotchas

- Current Claude models always think, and thinking counts against `max_tokens`. Keep limits generous
  (brief/script 16k, tagging 8k). `llm()` raises on `max_tokens` / `refusal` rather than saving half an answer.
- Some sites block GitHub's servers but work locally: OIES (403), GDELT (429). They're marked
  `local_only = true` in `sources.toml` and skipped when `GITHUB_ACTIONS` is set. Check a new feed both
  locally and in CI logs before relying on it. Most corporate and agency sites (OPEC, IEA, BP, Shell, FERC) block scripts.
- An episode is a full ElevenLabs request (~6-7k characters on a normal day). Credits are the binding cost;
  Claude is about $0.15-0.30 per run.

## Open items

- Skipping the episode on very thin days. Not decided yet.
