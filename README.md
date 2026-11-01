# clipper

Turn a YouTube podcast/video URL into vertical (9:16) short-form clips for Reels / Shorts / TikTok,
plus a `manifest.json` and a `review.html` page for human review.

It never downloads the full video: it reads the captions first, picks moments with a cheap LLM,
then downloads only those time ranges.

Two ways to use it: the **Clipper Studio** web app (below) or the `clipper` command line.

## Clipper Studio (web app)

```
frontend/   React + Vite UI: submit links, watch progress, review/approve clips, settings
backend/    FastAPI API + job worker that runs the clipper pipeline
clipper/    the pipeline library (shared by the web app and the CLI)
```

Only one step needs judgement: **choosing the moments** (where each clip starts and ends). Each job
picks who does that:

| Mode | How the moments are chosen |
|---|---|
| **Claude** | The backend writes a task folder (`backend/data/claude_tasks/<job>/`: `task.md`, `transcript.txt`, `heatmap.txt`). Claude reads it and writes `selection.json`. Uses your Claude subscription, no API key. |
| **API key** | Automatic two-pass LLM scoring with the key from Settings (Gemini, Anthropic or any OpenAI-compatible server). |

Everything else (transcript, partial download, face tracking, burned-in captions, encoding) is plain Python.
Claude's picks go through the same checks as the LLM's: start snapped to the hook's first word, end on a
sentence boundary, length limits, no overlaps.

**How Claude picks up a job** (any of these just writes `selection.json`; the worker continues within ~2 s):

1. **Automatic**: with "Launch Claude Code automatically" on (Settings), the backend runs
   `claude -p` headless in the task folder. It finds the CLI on PATH, or the copy bundled with the
   Claude desktop app.
2. **On demand**: in a Claude Code session opened in this repo, run `/clip-jobs`.
3. **Scheduled**: create a scheduled task/routine in the Claude app that runs `/clip-jobs` in this
   folder every 15 minutes.

A waiting job can also be switched to API-key mode from its page.

### Posting to many accounts: categories

The full lifecycle runs without anyone watching: link in, clips out, posts live on every channel in the niche.

1. **Connect accounts** (Accounts & categories page): any number per platform (YouTube Shorts, TikTok,
   Instagram Reels). Each "Connect account" opens that platform's own sign-in in a popup. You sign in
   yourself, and the app only receives OAuth tokens, encrypted at rest. Each platform first needs a developer
   app: see [docs/publishing.md](docs/publishing.md).
2. **Create categories** (Finance, Motivation, Entertainment, …) and tick the accounts that post each one.
   An account can belong to several categories. Per category:
   - **Approval**: *Fully automatic* (clips scoring at least the threshold are approved and posted as soon
     as they render, at most N per video), or *I approve each clip* (Approve on a clip card posts it).
   - **Minutes between clips** spaces out the clips of one video, plus hashtags added to every post,
     optional LLM-written per-platform captions, and content rules for choosing clips.
3. **Submit videos** with a category: one link, the **Batch** tab (one `url, category` per line), the
   API (`POST /api/batches`), or a `.txt`/`.csv` file dropped into `backend/data/inbox/` (picked up
   within 10 s; processed files move to `inbox/processed/`). The inbox and API keep URL sources open:
   any other tool or script can feed the queue.
4. **Track** everything on the **Posts** page: every clip × account as posted (with link), scheduled,
   in progress, failed (with Retry), or not posted yet. A clip is never posted twice to the same account,
   whatever triggers it.

Test the whole flow offline with the mock platform: start the backend with `PUBLISHER_MOCK=1`, connect a
few "Mock" accounts, and set Settings → API key mode → Mock to skip LLM costs.

### Run it

```bash
.venv/Scripts/pip install -e ".[dev]" -r backend/requirements.txt
```

```bash
npm --prefix frontend install
```

```bash
npm --prefix frontend run build
```

```bash
.venv/Scripts/python backend/run.py
```

Open http://localhost:8000. The backend serves the built UI, the API (`/api/...`, docs at `/docs`) and the
rendered clips (`/media/<job>/...`). For UI development, run `npm --prefix frontend run dev` as well and
open http://localhost:5173 (it proxies to the backend).

Data lives in `backend/data/` (`jobs/*.json`, `claude_tasks/`, `work/` cache, `output/<job>/` clips +
`manifest.json`). API keys entered in Settings are stored only in `backend/data/settings.json` and are never
sent back to the browser.

Backend tests: `cd backend` then `../.venv/Scripts/python -m pytest -q tests`.

## Setup

Python 3.11+ (3.12 recommended; MediaPipe does not support every new Python release yet), ffmpeg, and Node.js or Deno.

```bash
py -3.12 -m venv .venv
```

```bash
.venv/Scripts/pip install -e ".[dev]"
```

- **ffmpeg**: put it on PATH (e.g. `winget install Gyan.FFmpeg`). If it is missing, `pip install -e ".[ffmpeg]"`
  provides a bundled binary as a fallback.
- **JS runtime**: YouTube now requires one for format extraction. clipper uses `deno` if it is
  installed, otherwise `node`.
- **LLM key**: by default it uses Gemini Flash-Lite and reads `GEMINI_API_KEY`. See [LLM providers](#llm-providers).

## Usage

```bash
.venv/Scripts/python -m clipper "https://www.youtube.com/watch?v=VIDEO_ID"
```

Common options:

| Flag | Meaning |
|---|---|
| `-n 8` | number of clips (default 5) |
| `--candidates-only` | stop after choosing clips: no video download, writes `manifest.json` only |
| `--rules my_rules.txt` / `--no-rules` | campaign rules file (default `campaign_rules.txt`) |
| `--lang es` | caption language |
| `--min-sec 15 --max-sec 45` | clip length bounds |
| `--provider anthropic --model claude-haiku-5-5` | switch LLM |
| `--no-captions` | don't burn in captions |
| `--force [STEP]` | ignore caches from `transcript`, `candidates`, `download` or `render` onward |

Output goes to `output/<video_id>/`:

- `01_<title>.mp4` … 1080×1920 H.264/AAC clips with word-highlighted captions
- `01_<title>.jpg` thumbnail, `.ass` caption file (editable, re-render with `--force render`)
- `manifest.json` (per clip: title, hook line, reason, score + breakdown, heatmap value, source
  timestamps and YouTube link, transcript, reframe stats, and an empty `review` field for the reviewer)
- `review.html`: open in a browser to watch and compare every clip side by side

## How it works

1. **Transcript** (`transcript.py`): yt-dlp metadata only (no download). Uses manual subtitles in the
   target language if they exist, else YouTube auto-captions (the original `-orig` ASR track, not a
   machine translation). json3 gives word-level timings and `>>` speaker changes. The "Most Replayed"
   heatmap is saved when YouTube provides it. No captions means a clear error (Whisper fallback is a TODO).
2. **Candidates** (`candidates.py`):
   - *Pass 1*: overlapping 75 s windows (30 s stride), 6 per LLM call, scored 0–10 on hook strength,
     emotion, suspense/curiosity, controversy/surprise and standalone clarity. Strict JSON validation
     with one retry. `final_score` = weighted mean (weights in `config.yaml`) + a bonus on heatmap peaks.
   - *Pass 2*: the top 15 windows are merged into regions; the LLM picks the final N clips and their
     hook lines. Every pick is then checked locally: the start is aligned to the hook's exact first word,
     the end is snapped to a sentence end (or a pause/speaker change), the length is forced into 20–60 s,
     and overlapping picks are dropped. If the LLM returns too few valid clips, the best remaining regions fill the gap.
   - Campaign rules are injected into both passes as hard constraints; windows that break them are excluded.
3. **Download** (`download.py`): `yt-dlp --download-sections "*start-end" --force-keyframes-at-cuts`,
   ≤1080p, 0.5 s padding, 3 in parallel, 2 retries; a file without audio counts as a failure.
4. **Render** (`reframe.py`, `captions.py`, `render.py`): MediaPipe face detection at ~3 fps, camera
   cuts found by colour-histogram jumps, and one steady crop per shot centred on the dominant face
   (centre crop when there is no face). Captions are an ASS file, 3 words per line with the spoken word
   highlighted. One ffmpeg pass does trim + crop + scale + captions + encode.

Everything is cached in `work/<video_id>/` (info, transcript, per-window scores, clips, raw downloads,
face analysis). Re-running resumes where it stopped: an interrupted or rate-limited scoring pass
retries only the missing windows. A fully cached re-run takes about a second.

Approximate cost: a 2-hour episode is ~45 LLM calls, ~110k input / ~25k output tokens on Flash-Lite.
End-to-end time is ~3 minutes, mostly the partial downloads.

## LLM providers

Set via env vars (override `config.yaml`):

| `LLM_PROVIDER` | default `LLM_MODEL` | key (`LLM_API_KEY` or…) |
|---|---|---|
| `gemini` (default) | `gemini-flash-lite-latest` | `GEMINI_API_KEY` |
| `anthropic` | `claude-haiku-5-5` | `ANTHROPIC_API_KEY` |
| `openai` | `gpt-5-mini` | `OPENAI_API_KEY`; set `LLM_BASE_URL` for any OpenAI-compatible server (Ollama, LM Studio, vLLM) |
| `mock` | n/a | none: offline keyword heuristic, for free dry runs of the pipeline |

Free-tier Gemini keys are rate-limited; clipper backs off, skips what still fails, and retries
those windows on the next run. Lower `llm.concurrency` if you hit 429s often.

## Tests

```bash
.venv/Scripts/python -m pytest
```

## Known limitations (MVP)

- Two people in one wide shot: the crop follows the larger face, not necessarily whoever is speaking.
- No Whisper fallback for videos without captions.
- Auto-caption punctuation is imperfect, so a few clip ends land on a pause rather than a full stop.
