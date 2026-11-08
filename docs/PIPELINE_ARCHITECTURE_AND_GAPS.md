# Clipper Studio: Architecture, Gaps and Implementation Plan

Status: working document, 5 Nov 2026. Covers every item in the pipeline audit table (items 1 to 34) that is
Partial, Pending or Gap, plus the Done items that still have a known caveat.

Every external fact below was checked against the platform's own documentation where that was possible, or
against a named secondary source, and each one is marked so you can tell them apart:

- **[OFFICIAL]**: read on the platform's own documentation page during this research.
- **[SECONDARY]**: from a vendor blog, forum or third-party guide. Verify before relying on it.
- **[VERIFY]**: not confirmed. Check the page named before building on it.

---

## 0. How to read this document

| Status | Meaning |
|---|---|
| Done | Works and was tested on this machine |
| Done (code) | Code exists and unit tests pass, but no live end-to-end run yet |
| Partial | Works in some cases; a known limit or external blocker remains |
| Pending | Built, but needs a setup step only the owner can do (accounts, keys, approvals) |
| Gap | Missing or wrong against the stated rules; must be fixed before publishing |

Each item below has: **Current state** (evidence in the code or live system), **Goal**, **Options researched**,
**Decision**, **Steps**, **Fallbacks** (what to do when a step fails), **Errors to prevent**, **Done when**,
**Resources**, and **Your decision needed** where the owner has to choose.

---

## 1. Architecture overview

```
 INPUT                TRANSCRIPT           PICK MOMENTS            CLIP PROCESSING                REVIEW & PUBLISH
 ┌───────────────┐    ┌───────────────┐    ┌───────────────────┐   ┌────────────────────────────┐   ┌──────────────────────┐
 │ YouTube link  │ -> │ YouTube       │ -> │ Claude (task file)│ ->│ download ranges (yt-dlp)   │ ->│ human review         │
 │ upload (PC)   │    │ captions      │    │ LLM (API key)     │   │ or cut uploaded file       │   │ approve / reject     │
 │ batch / inbox │    │ or Whisper    │    │ action picks      │   │ face reframe 9:16          │   │ trim / notes         │
 └───────────────┘    └───────────────┘    └───────────────────┘   │ captions, branding, music  │   └──────────┬───────────┘
                                                                  │ render (ffmpeg)            │              │ approval = human push
                                                                  └────────────────────────────┘              v
                                                                                                    ┌──────────────────────┐
                                                                                                    │ publish queue        │
                                                                                                    │ clip x account pairs │
                                                                                                    │ spacing, schedule    │
                                                                                                    └──────────┬───────────┘
                                                                                                               v
                                                                                       YouTube Shorts | TikTok | Instagram Reels
```

Runtime: one FastAPI backend (`backend/run.py`, port 8000) with a background worker thread; one React frontend
(Vite on port 5173 in development; Vercel in production). The frontend talks to the backend through `/api` and
`/media` (proxied in development, rewritten by `vercel.json` in production).

### 1.1 Data stores (all JSON files on disk)

Root: `backend/data/` (override with the `CLIPPER_DATA` environment variable).

| Store | Path | Written by | Read by | Notes |
|---|---|---|---|---|
| Jobs | `data/jobs/<id>.json` | job store | UI, worker | One file per job; `running` jobs resume as `queued` after a restart |
| Work cache | `data/work/<video_id>/` | transcript, download, render | worker | `info.json`, `transcript.json`, `clips.json`, `raw/`, `analysis/`, `branding/` |
| Output | `data/output/<job_id>/` | render | UI, publisher | `.mp4`, `.jpg`, `.ass`, `.render.json` cache key, `manifest.json` |
| Uploads | `data/uploads/` | `/api/uploads` | worker | Video plus `.meta.json` |
| Branding | `data/branding/` | `/api/branding` | worker | Images plus `brand.json` defaults |
| Music library | `backend/assets/music/` | owner (drop files) | worker | `tracks.json` tags; audio files git-ignored |
| Claude tasks | `data/claude_tasks/<job>/` | `claude_bridge` | Claude Code | `task.md`, `selection.json` |
| Categories | `data/categories.json` | `catalog.Categories` | UI, auto-publisher | Approval mode, accounts, rules |
| Publisher | `data/publish/` (`PUBLISHER_DATA`) | publisher | publisher | Publish jobs, posts, accounts, OAuth pending |
| Accounts | `data/publish/accounts/<id>.json` | `publisher.accounts` | connectors | Tokens encrypted at rest (Fernet) |
| Settings | `data/settings.json` | settings store | worker, LLM | LLM provider, default mode, Claude command |

### 1.2 Cross-cutting rules (apply to every item below)

These rules are the reason the pipeline should not produce errors in future. They are requirements for every
new change, not optional polish.

1. **One error taxonomy.** Every failure is classified into exactly one class, and each class has one behaviour:

   | Class | Examples | Behaviour |
   |---|---|---|
   | `ConfigError` | missing key, missing env var, ffmpeg not found | Stop before doing any work; say which setting to fix |
   | `TransientError` | timeout, 5xx, network reset | Retry with exponential backoff (max 3), then fail the step |
   | `RateLimitError` | 429, quota exceeded, daily cap | Do not retry now; record `retry_after`; resume later |
   | `BlockedError` | YouTube "not a bot" check | Stop all requests to that host for the job; ask the owner |
   | `AuthError` | token invalid, scope missing | Mark the account "reconnect needed"; no retries |
   | `ContentError` | no audio, unreadable video, invalid pick | Fail that clip only; show the reason on the clip |

   This is already partly built: `ClipperError` plus the `retryable` flag in the publisher, and the bot-check
   stop added in this round. The remaining work is to use the same classes everywhere.

2. **Never retry a blocked or quota request immediately.** Retries make a block last longer. Retries are only for
   transient errors, and each retry waits longer than the last.

3. **Circuit breaker per platform.** After N consecutive failures of one kind (e.g. 3 auth errors, or any block),
   pause that platform, keep the queue, and show one clear message in the UI.

4. **Limits are checked before the call.** The platform limits (duration, file size, daily posts, aspect ratio)
   are already declared in `publisher/connectors/*.py`. Every publish must check them first and return a
   `ContentError` with the limit, instead of sending a request the platform will reject.

5. **Idempotency.** A publish is identified by (clip, account). It is never queued twice (already true in
   `catalog.AutoPublisher`). Long platform operations store their remote id (for example an Instagram container id)
   so a restart resumes instead of re-uploading.

6. **The human gate is an invariant, enforced in one place.** A clip is published only if
   `review.approved is True` and `approved_by` is a person, not `"auto"`. This must be a single function with
   tests, called by every publish path. (See item 28.)

7. **Start-up self-check.** On start, the backend lists each platform as `ready`, `needs setup` or `blocked`, with
   the exact missing setting. The UI already shows parts of this (`/api/publish/platforms`); extend it to the LLM
   key, ffmpeg, free disk space and the music library.

8. **Staged rollout.** A new platform runs in mock mode first (`PUBLISHER_MOCK=1`), then private or inbox mode
   against a real account, then public. Nothing goes public until the private run passed.

9. **Redaction.** Tokens, client secrets and API keys never appear in logs, job logs, error messages or the UI.
   Errors are cut to a safe length and scrubbed before they are stored.

10. **Tests with recorded responses.** Each connector gets contract tests that replay recorded platform responses
    (success, each error code, the retry path). Live tests are a manual checklist, not part of CI.

11. **Backups.** The data folder is the only copy of jobs, approvals and encrypted tokens. Back it up before every
    upgrade and before any migration (item 28 changes stored categories).

---

## 2. Input, transcript and selection

### 2.1 Item 12 (and item 1): YouTube download blocked, input from links is Done with an upload fallback (YouTube links stay best-effort)

**Current state.** The clip download runs `yt-dlp` as a subprocess for each clip range (`clipper/download.py`).
On 5 Nov 2026 YouTube answered job `2e8da1e8cb` with "Sign in to confirm you're not a bot". Transcript and captions
still worked. The bot check now stops retries at once and tells the owner what to do (this round). The app also
keeps no cooldown, so a second attempt an hour later hits YouTube again.

**Goal.** Every video can be completed, through a route the owner is allowed to use, without retry storms and
without any attempt to get around YouTube's checks.

**Options researched.**

| Option | Cost | Verdict | Source |
|---|---|---|---|
| A. Owner uploads the video file ("Upload from this PC") | Free | Works today; the file must be the same video (check duration) | Code: `main.py` upload limit 20 GB |
| B. YouTube Data API for metadata and publishing | Free within quota (10,000 units/day default) | Official, but the Data API does not provide video downloads | [OFFICIAL] https://developers.google.com/youtube/v3/determine_quota_cost |
| C. yt-dlp with the owner's own signed-in session (cookies) | Free | Upstream's own FAQ suggests it. It acts with the owner's account and carries account risk. Owner-initiated only | [OFFICIAL] yt-dlp FAQ, linked from the error text: https://github.com/yt-dlp/yt-dlp/wiki/FAQ |
| D. Wait and retry later, with a cooldown | Free | Works if the block lifts; never guaranteed | Owner observation |
| E. Only videos the owner has rights to | Free | Safest; limits the catalogue to the owner's own channel | Policy decision |

**Decision.** A is the primary route, D with a cooldown second. C is off by default and only available as a
conscious owner choice with a warning. We do not build automatic ways around YouTube's checks. Whether downloading YouTube
media is allowed for the owner's use is a legal question the owner must answer (see Section 6).

**Steps.**
1. Owner decides the download policy (Section 6, decision 1).
2. On a `BlockedError`, store `blocked_until = now + 6 h` on the job (configurable). Do not request that video again
   before then. The job card shows a countdown.
3. On a blocked job, show "Use my file": the owner picks the same video as a file. The job switches its source to the
   upload (the picks and transcript stay). Reject the file if its duration differs from the YouTube duration by more
   than 1 s, and show both durations.
4. Keep the bot-check stop from this round for the other clips of one job (already built).
5. Add a `blocked` state to the job status list so the UI can show it separately from `failed`.
6. Option C, only if the owner asks for it: a setting that points yt-dlp at a cookies file the owner exports
   themselves. It is off by default and never stored in logs.

**Fallbacks.**
- Blocked, no file, cooldown not over: show the countdown and keep the job waiting. No request is sent.
- Blocked after the cooldown: fail the job with the plain message and keep the picks, so the owner can upload later.
- Uploaded file has the wrong duration: reject it, keep the job blocked.
- Upload fails half way: the upload stores in chunks and resumes; the job is untouched.

**Errors to prevent.** Retry storms (fixed: no retry after a bot check). Wrong file (duration check). Cookies or
tokens in logs (redaction rule 9). Silent bypass (Option C is never automatic).

**Done when.** A blocked job can be finished with an uploaded file in three clicks. After a block, no request for that
video is sent for 6 hours. A recorded bot-check response in a test produces exactly one request and the `blocked`
state.

**Resources.**
- yt-dlp FAQ (linked from the error text): https://github.com/yt-dlp/yt-dlp/wiki/FAQ [OFFICIAL]
- YouTube Data API quota costs: https://developers.google.com/youtube/v3/determine_quota_cost [OFFICIAL]
- YouTube Terms of Service: read before deciding Section 6 item 1 [VERIFY the current text]

**Your decision needed.** Section 6, decision 1 (download policy).

---

### 2.2 Item 8: LLM picks need a key (Partial)

**Current state.** `llm_api_key` is empty in settings and no environment variable is set. Claude mode works because the
Claude CLI is installed (`Claude CLI` shows green in the header). API-key jobs fail with
"No API key for gemini". A test endpoint exists (`POST /api/settings/test-llm`) but was not run successfully.

**Goal.** At least two working ways to pick moments, so one missing key or rate limit never stops the pipeline.

**Options researched.**

| Option | Cost | Verdict | Source |
|---|---|---|---|
| Claude mode (Claude Code CLI, task file) | Uses the owner's Claude subscription | Works now; the default | In code (`claude_bridge.py`) |
| Gemini API key from Google AI Studio | Free tier exists; per-model limits must be checked | Free tier; limits on the AI Studio page | [OFFICIAL] https://aistudio.google.com/docs/rate-limits (links to the per-model table at https://aistudio.google.com/rate-limit [VERIFY numbers]) |
| Gemini free-tier model list | n/a | Sources disagree on whether 2.5 Pro is free | [SECONDARY] several 2026 blogs disagree |
| Paid OpenAI or Anthropic API key | Paid per token | Works; pricing must be checked | [VERIFY] provider pricing pages |
| Local OpenAI-compatible server (Ollama, LM Studio) | Free, uses the PC | Supported by the code (`LLM_BASE_URL`); quality and speed depend on the PC | In code (`config.yaml` `base_url`) |

**Decision.** Keep Claude mode as the default. Add a Gemini key as the second route once its free limits are confirmed on
the AI Studio page. Ollama is the offline fallback.

**Steps.**
1. Owner creates the key (or picks another provider).
2. Save it in Settings (stored locally) or set `LLM_API_KEY`.
3. Run the test endpoint; it must return OK before the default mode may be `api`.
4. On a `RateLimitError` in API mode, the job shows "Run with Claude instead" (a reverse of the existing "Switch to API
   key" button).
5. Record the provider and model used in each job (already in `selection`).

**Fallbacks.** Claude mode, then Ollama, then action picks (only for no-speech videos). A job never ends without a
choice; it asks the owner.

**Errors to prevent.** Missing key (test endpoint before use). 429 (backoff, then offer the other route). Invalid JSON
from the model (already validated and retried). Missing `music_mood` (defaults to calm, already done).

**Done when.** The test endpoint returns OK. A 60-second test video completes in API mode. A forced 429 in a test
produces the offer, not a crash.

**Resources.**
- Google AI Studio rate limits: https://aistudio.google.com/docs/rate-limits [OFFICIAL]; per-model table: https://aistudio.google.com/rate-limit [VERIFY]
- Free-tier model eligibility: conflicting 2026 blogs [SECONDARY]; confirm on the AI Studio page
- Ollama and LM Studio: [VERIFY] official sites before adopting

**Your decision needed.** Which provider to add (Section 6, decision 4).

---

### 2.3 Item 11: Selection quality (Partial)

**Current state.** Scores come from the model itself (`score_breakdown`). The 7+ auto threshold uses those numbers. The
"actionable" preference is listed but not scored. There is no check that a clip makes sense without the sentence before
it (standalone clarity is only a score). Sentence ends and untimed lines were fixed this round.

**Goal.** A clip that is complete, understandable without context, and matches the rules. Scores that track what the
owner actually approves.

**Options researched.**

| Option | Cost | Verdict | Source |
|---|---|---|---|
| Score "actionable" as a criterion | Free | Cheap and immediate | In code (`CRITERIA`, task rubric) |
| Calibrate scores against the owner's approve/reject labels | Free, needs ~50 labels | Turns the threshold into a measured number | [SECONDARY] LLM-judge calibration guides, e.g. https://mlflow.org/articles/llm-judge-calibration/ |
| Randomise candidate order and average | Extra calls | Reduces position bias in rubric scoring | [SECONDARY] arXiv 2602.02219 (pointwise vs pairwise judging): https://arxiv.org/html/2602.02219v1 |
| Several judge models and a majority vote | More cost | Plausible; the cost and accuracy claims are unverified | [SECONDARY] only |
| Reason first, then score (G-Eval style) | Same cost | Documented method; the original paper must be checked | [VERIFY] Liu et al., G-Eval (arXiv 2303.16634) |

**Decision.** (1) Score "actionable". (2) Store the owner's approve or reject as a label on every clip. (3) Randomise
candidate order in pass 1 and keep the reasoning. (4) Calibrate the threshold after 50 labels. (5) Add the context check.

**Steps.**
1. Add `actionable` to `CRITERIA`, the task rubric and the weights. Test: weights sum to 1.
2. Context check: if the first sentence refers back ("this", "that", "these", "he said", "the study") and the 20 s before
   name nothing, move the start to the nearest earlier sentence start, up to 30 s back, never past `max_sec`. Test on
   job `9ea30dc527`: expected start 256.4 s instead of 268.4 s.
3. Accuracy rule in the prompt: titles and claims must match what was said; no invented numbers.
4. Store `label_source` and a reason tag on each review (approve, reject, and why).
5. Calibration script: precision at the top 5 against the owner's labels; report the threshold that matches the owner.

**Fallbacks.** Fewer than 50 labels: keep threshold 7 and manual review. Context check finds no earlier start: keep the
clip and flag "may need context" on its card.

**Errors to prevent.** Clips longer than `max_sec` after the context extension (hard cap). Overlaps with other picks
(existing check). Exaggerated titles (accuracy rule).

**Done when.** `actionable` appears in every pick's breakdown. The context-check test passes on job `9ea30dc527`.
A calibration report exists after 50 labels.

**Resources.**
- G-Eval, Liu et al. 2023: arXiv 2303.16634 [VERIFY]
- Pointwise vs pairwise judging: https://arxiv.org/html/2602.02219v1 [SECONDARY]
- LLM-judge calibration: https://mlflow.org/articles/llm-judge-calibration/ [SECONDARY]

**Your decision needed.** Should "actionable" be a hard rule or a preference? Today it is a preference (Section 6,
decision 9).

---

### 2.4 Auto-caption transcript quality (ties to item 5, Done with a caveat)

**Current state.** Cleaning is done (arrows, sound tags, repeats, untimed lines), this round. About 17% of lines in
job `9ea30dc527` had no word timings. Captions for those lines are spread evenly, so their timing can drift. The
cleaning cannot fix wrong words.

**Goal.** When the captions are unreliable, offer a better transcript from the audio, without breaking the owner's
rights rule (2.1).

**Options researched.**

| Option | Cost | Verdict | Source |
|---|---|---|---|
| Keep YouTube captions, cleaned (current) | Free | Good enough for most clips; wrong words stay | In code |
| faster-whisper (local Whisper) | Free, MIT licence; Python 3.9 or newer; CUDA 12 only for GPU | Already installed and used for uploads | [OFFICIAL] https://github.com/SYSTRAN/faster-whisper |
| WhisperX (word alignment, speaker labels) | Free; licence not confirmed here | Useful for alignment; verify the licence first | [VERIFY] https://github.com/NorSC/whisperX |
| pyannote speaker diarization (item 13) | Free; code MIT, model needs a Hugging Face token and accepted terms | Same tool serves items 13 and this one | [OFFICIAL] https://github.com/pyannote/pyannote-audio |

**Decision.** Keep cleaned captions. Add a quality check that flags a transcript when more than 15% of lines are
untimed or repeats are frequent. The flag offers "Re-transcribe with Whisper". For YouTube videos this needs the audio,
so it follows the same rule as item 12 (the owner's file).

**Steps.**
1. Compute the quality numbers at load (`untimed_share`, `repeat_count`, `arrow_count`) and store them in
   `transcript.json`.
2. Show a badge on the job when the numbers are over the limits.
3. "Re-transcribe" runs faster-whisper on the owner's file and replaces the transcript cache (keep the old one).
4. Test with job `9ea30dc527`: the badge shows; re-transcription (on a file) removes the repeated phrase.

**Fallbacks.** No file: keep the cleaned captions and the badge. Whisper fails (memory, CUDA): CPU int8 (already the
default), then the cleaned captions.

**Errors to prevent.** Replacing a good transcript with a worse one (keep the old cache and compare word counts).
Re-transcribing the wrong file (duration check).

**Done when.** The badge appears on the test job. A re-transcription of an uploaded file reduces repeats to zero on the
test case.

**Resources.** faster-whisper [OFFICIAL] (above); pyannote [OFFICIAL] (above).

---

### 2.5 Item 13: Two-person podcasts are framed poorly (Partial)

**Current state.** Faces are detected (MediaPipe BlazeFace, with a Haar fallback). The crop follows the largest face in
each camera shot. A second speaker can be cut off, and there is no audio speaker information.

**Goal.** A two-person podcast clip shows both speakers when both talk, and one speaker otherwise.

**Options researched.**

| Option | Cost | Verdict | Source |
|---|---|---|---|
| A. Largest face per shot (current) | Free | Fails on two-person shots | In code (`reframe.py`) |
| B. Split screen (two faces stacked) when both are on screen and both talk | Free | Good visual result; needs B's speaker information | In code concept (`layouts.py` was an earlier design) |
| C. Audio speaker diarization (pyannote `speaker-diarization-community-1`) | Free; code MIT; model needs a Hugging Face token and accepted conditions | Gives who speaks when | [OFFICIAL] pyannote README: https://github.com/pyannote/pyannote-audio |
| D. Visual active-speaker detection: TalkNet, LR-ASD, Light-ASD | Free; licences need checking | Matches face to voice from video | TalkNet listed MIT by a third-party index [SECONDARY] https://github.com/TaoRuijie/TalkNet_ASD; LR-ASD https://github.com/airmeet/LR-ASD and Light-ASD https://github.com/Junhua-Liao/Light-ASD: licences [VERIFY] |
| E. Paid speaker-tracking APIs | Paid | Not chosen | n/a |

**Decision.** B with C first. D only if B and C are not enough, after the licences are verified.

**Steps.**
1. Diarize each video once and cache speaker turns (`work/<id>/speakers.json`).
2. Per clip, decide the layout: SINGLE (one face on screen for most of the clip), SPLIT (two faces, both speak), or the
   existing FIT fallback when text would be cut.
3. In SPLIT, stack the two face crops (each half of the 9:16 frame) and keep the crop centred on each face.
4. Test on a two-person video: both faces are visible in the output whenever both speak.

**Fallbacks.** No Hugging Face token: use the face-count heuristic (two faces in more than 40% of frames gives SPLIT).
Face detector finds nothing: centre crop (already). Model download fails: Haar cascade (already).

**Errors to prevent.** SPLIT showing a silent person (use diarization). Tiny faces treated as people (minimum size).
Model downloads at render time (cache them in the work folder before the job).

**Done when.** On the owner's two-person test video, both speakers are in frame whenever both speak. The job still runs
without a token.

**Resources.** pyannote [OFFICIAL]; community model page on Hugging Face [VERIFY link]; WhisperX [VERIFY licence];
TalkNet, LR-ASD, Light-ASD [VERIFY licences] (links above).

**Your decision needed.** Create a free Hugging Face account and accept the diarization model terms (Section 6,
decision 6).

---

### 2.6 Item 14: One caption style (Done, enhancement)

**Current state.** One word-highlight style (ASS, `clipper/captions.py`). Caption placement is 27% from the bottom, and
the fit layout places them in the top band.

**Goal.** Two or three presets the owner can pick per job or per brand.

**Steps.**
1. Add `render.caption_style` with three values: `highlight` (current), `box` (solid background), `plain`.
2. Keep `highlight` as the default. Unknown values fall back to it.
3. Add a select in the job form and the Branding page. Render one test clip per style and check it.

**Fallbacks.** Unknown style: highlight. **Done when:** three renders checked by eye and one unit test per style.

**Resources.** ASS/libass subtitle format [VERIFY the libass documentation].

---

### 2.7 Item 15: Burned-in caption detection (Done, tested only on synthetic video)

**Current state.** OCR reads on-screen text once per second and matches it to speech (`clipper/source_text.py`,
RapidOCR installed). Tested on a synthetic clip with a drawn caption and on a generated burned-in video. Not tested on
real burned-in videos from YouTube.

**Goal.** Reliable detection on real videos with few false positives (HUD text, titles, logos).

**Options researched.** RapidOCR (installed; its licence must be checked [VERIFY]); Tesseract (not installed; needs a
native binary). Keep RapidOCR.

**Steps.**
1. Collect 3 real videos with burned-in captions and 3 without (owner's files).
2. Measure detected seconds against the ground truth typed by hand for 2 minutes of each video.
3. Tune `MATCH` and the caption band if precision is below 90%.
4. Store the numbers in this document's test log.

**Fallbacks.** Detection poor: set `render.source_captions: off` (the job then behaves as before). Engine missing: detection
is skipped with a logged warning (already).

**Done when:** precision and recall are measured on the six videos and documented.

---

### 2.8 Item 17: Background music (Done in code, no tracks)

**Current state.** Library, mood matching, ducking and the Branding-style render are built and tested with a synthetic
track. The owner has added no tracks, so the option is empty.

**Goal.** A library of tracks whose licence is known for every track, so nothing is used without the right to use it.

**Options researched.**

| Source | Licence facts | Verdict | Source |
|---|---|---|---|
| YouTube Audio Library | Tracks won't receive a Content ID claim; some tracks need attribution | Use, record the attribution per track | [OFFICIAL] https://support.google.com/youtube/answer/13486873 |
| YouTube Shorts Audio Library | Up to 90 s of music in a 3-minute Short; Shorts over 1 min with an active claim are blocked | Use for native Shorts; not a portable licence for our app | [OFFICIAL] same page |
| Pixabay music | No attribution required; standalone distribution not allowed; trademarks restricted; Content ID not addressed | Usable with care; record the licence page | [OFFICIAL] https://pixabay.com/service/license/ |
| Free Music Archive, Incompetech, Epidemic Sound | Not verified in this round | Check each before use | [VERIFY] |

**Decision.** Start with YouTube Audio Library and Pixabay tracks the owner downloads. Every track needs a `licence`
field in `tracks.json`; a track without one is listed but not used.

**Steps.**
1. Owner downloads 5 to 10 instrumental tracks and records the licence and source link for each.
2. Add entries to `assets/music/tracks.json` (name, moods, instrumental, licence, source).
3. Startup check: list tracks with no licence field as a warning.
4. Render one branded clip with music and the owner listens to it.
5. Keep a dated screenshot of each licence page in the owner's records.

**Fallbacks.** No tracks: the option stays empty (already). A track that fails to decode: skipped with a warning.

**Errors to prevent.** Using a track without a licence (validation). Vocal tracks under speech (instrumental first,
already). Loudness jumps (check with a loudness meter [VERIFY the tool]).

**Done when:** 10 licensed tracks, each with a record, and one listened-to clip.

**Resources.** YouTube support page [OFFICIAL]; Pixabay licence [OFFICIAL]; the two above.

---

## 3. Publishing and operations

Publishing is where a mistake costs the most: a post goes public, and a deleted post is still seen. Every item in this
section follows the rules in 1.2, and the human gate (item 27) is checked before any platform call.

### 3.1 Item 22: Categories have not been created (Pending)

**Current state.** The live system has 0 categories (`GET /api/categories`). The category page exists
(`CategoriesPage.tsx`). Approval modes are `manual` (default) and `auto` (see item 28). A category holds its accounts,
rules, minimum score, maximum clips per video, and spacing between posts.

**Goal.** One category per channel or brand, each with the rules and accounts that apply to it.

**Options researched.** No external research needed; this is configuration. The data model is in `backend/app/catalog.py`.

**Steps.**
1. Create one category per channel. Set approval to `manual`, and tick the channel's accounts (they are pre-ticked when you post its clips).
2. Paste the campaign rules for that channel into the category (hard limits and preferences, as in the job form).
3. Connect the accounts (item 26) before adding them to the category.
4. Test: create a job in the category, approve one clip, and check the publish preview. Use mock mode first
   (`PUBLISHER_MOCK=1`).

**Fallbacks.** No category: the clips stay for manual download (existing behaviour, labelled "No category (review and
publish by hand)").

**Errors to prevent.** A category pointing to a disconnected account (warning already shown). Duplicate names (the slug
must be unique; check on create). Old `auto` values (item 28 migration).

**Done when.** One category with one connected account completes a private test publish.

**Resources.** Code only.

---

### 3.2 Item 23: YouTube Shorts is not configured (Pending)

**Current state.** The connector is written (`publisher/connectors/youtube.py`): OAuth 2.0, resumable upload, privacy
default `private`, `#Shorts` required, 100 uploads per day declared. It is not configured: `/api/publish/platforms` reports
`configured: false` and asks for `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`. The redirect URI is
`http://localhost:8000/api/publish/oauth/youtube/callback`.

**Goal.** The owner's channel accepts a private upload from the app, then a public upload after the owner approves it.

**Facts that shape the design.**
- `videos.insert` costs 1 quota unit per call, is capped at 100 calls per day per project, and has its own bucket. The
  rest of the API gets 10,000 units per day by default. [OFFICIAL] https://developers.google.com/youtube/v3/determine_quota_cost
- Projects that are not verified by Google can be limited to 100 new users. Upload scopes are sensitive scopes, so
  verification is required before wider use. [SECONDARY] Google Help pages (found by search, not fetched):
  https://support.google.com/cloud/answer/7454865 [VERIFY]
- An app left in "Testing" gets refresh tokens that expire after 7 days. [SECONDARY] connector comments and vendor guides.
- Uploads from unverified projects may be forced to private. [SECONDARY] a 2020 notice reposted on a forum. [VERIFY]
- Scheduled public release may be set with `status.publishAt` on a private video. [VERIFY] in the YouTube API reference.
- The YouTube audit guide returned 404 during research. Find the current URL before relying on it. [VERIFY]

**Options researched.**

| Option | Verdict |
|---|---|
| A. Keep the app in Testing, with the owner as test user | Right for the owner's own channel. Tokens expire every 7 days, so reconnect weekly until the app is published |
| B. Publish the consent screen and request verification | Needed for other users or more than 100 users. Takes time; sensitive scope |
| C. Manual upload with a generated caption file | Always works; no API. Fallback for every failure |

**Decision.** A now. B when another person needs to connect. C as the permanent fallback.

**Steps.**
1. Create a Google Cloud project; enable the YouTube Data API v3.
2. Configure the OAuth consent screen (External). Add the scopes the connector asks for. Add the owner as a test user.
3. Create an OAuth client of type **Web application**. Add the redirect URI exactly as `/api/publish/platforms` shows it.
4. Put the client ID and secret in `.env` (see `docs/publishing.md`) and restart the backend.
5. Check `/api/publish/platforms`: `configured` must be `true`.
6. Connect the channel from the Accounts page.
7. Upload one clip as **private**. Check it in YouTube Studio, then delete the test video.
8. Only after step 7 may a clip be made public.

**Fallbacks.**
- Quota reached: the publish waits until the quota resets (rule 2); the queue stays intact.
- Token expired (Testing): show "reconnect needed" on the account; no retries.
- Upload interrupted: the resumable upload continues from the last byte (already built).
- Unverified-app screen: accept it as the test user.

**Errors to prevent.** Redirect URI mismatch (copy it from the platforms endpoint). Wrong client type (must be Web). Missing
scope (the owner must re-consent). Public upload by mistake (privacy default stays `private` until approved). A 403
`quotaExceeded` treated as a failure (it is a `RateLimitError`).

**Done when.** A private upload appears in YouTube Studio. The token refreshes after a backend restart. A forced quota
error results in a queued retry, not a failed job.

**Resources.**
- Quota costs [OFFICIAL]: https://developers.google.com/youtube/v3/determine_quota_cost
- OAuth verification help [SECONDARY, reached by search]: https://support.google.com/cloud/answer/7454865
- Music and Shorts rules [OFFICIAL]: https://support.google.com/youtube/answer/13486873

**Your decision needed.** Whether to request verification later (Section 6, decision 5).

---

### 3.3 Item 24: TikTok is not configured and posts stay private (Pending)

**Current state.** The connector is written (`publisher/connectors/tiktok.py`): scopes `user.info.basic`,
`user.info.profile`, `video.publish`, `video.upload`. Posting uses inbox upload (the owner finishes the post in the TikTok
app) or direct post. Privacy is forced to `SELF_ONLY` until `TIKTOK_AUDITED=1` is set after TikTok's audit. Credentials
are `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`.

**Goal.** The owner can post a clip to TikTok through the app, through a route TikTok allows for the app's audit status.

**Facts that shape the design.**
- Until an app passes TikTok's audit, its posts stay private (`SELF_ONLY`). TikTok's documentation says the audit is needed
  to lift the visibility restriction. [OFFICIAL snippet, developers.tiktok.com; the page returned 503 when fetched, so
  re-check it] https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
- Inbox upload (`MEDIA_UPLOAD`) puts the clip in the owner's TikTok inbox to finish. It needs TikTok app version 31.8 or
  newer. [OFFICIAL snippet] https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
- A draft left unfinished for 24 hours is discarded. [SECONDARY] Postiz docs https://docs.postiz.com/self-host/providers/tiktok
- Unaudited clients may post for at most 5 users in 24 hours. Sources disagree on the wording. [SECONDARY]
- A daily cap on posts is returned as `spam_risk_too_many_posts`. [OFFICIAL snippet]

**Options researched.**

| Option | Verdict |
|---|---|
| A. Inbox upload now | Works without audit; the owner taps to finish. Fallback for ever if the audit fails |
| B. Direct post, private (`SELF_ONLY`) | Works without audit; nobody else sees the post |
| C. Pass the audit, then direct post publicly | The goal; needs a demo and a review |

**Decision.** A now. Apply for the audit. Keep `TIKTOK_AUDITED` unset until TikTok approves in writing.

**Steps.**
1. Create a TikTok developer app. Add Login Kit and the Content Posting API.
2. Set the redirect URI and the two environment variables.
3. Connect the owner's account. Check the scopes shown on consent.
4. Send one clip through inbox upload. Check that it appears in the TikTok app inbox and can be finished there.
5. Record a short demo of the flow for the audit, as TikTok asks. Submit the audit.
6. After written approval only, set `TIKTOK_AUDITED=1`.

**Fallbacks.**
- Audit refused: keep inbox mode. The app stays useful.
- Daily cap error: queue for the next day.
- Draft not finished within 24 hours: notify the owner before the window closes; the app should schedule inbox uploads close
  to the time the owner will be available (see item 29).

**Errors to prevent.** Public post from an unaudited app (the connector already forces private; keep a test for it).
Missing scope (re-consent). Chunk upload failure (chunked upload exists; test with a large file).

**Done when.** A clip appears in the owner's TikTok inbox. The account reconnects after a restart.

**Resources.**
- TikTok Direct Post reference [OFFICIAL, re-check]: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
- TikTok photo post reference (inbox mode) [OFFICIAL snippet]: https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
- Postiz TikTok notes [SECONDARY]: https://docs.postiz.com/self-host/providers/tiktok

---

### 3.4 Item 25: Instagram Reels is not configured (Pending)

**Current state.** The connector is written (`publisher/connectors/instagram_unified.py`, Unified Login): a container with
`media_type=REELS` and a `video_url`, polling `status_code`, then `media_publish`. It needs a **public** video URL, which
comes from `PUBLISHER_PUBLIC_BASE_URL` or a tunnel. The tunnel is off. Privacy is `public` only.

**Goal.** A Reel goes to the owner's professional Instagram account from the app, after the owner approves it.

**Facts that shape the design.**
- A REELS container is created with `media_type=REELS` and the `video_url`. Meta fetches the file itself, so it must be on
  a public server at the time of each attempt. [OFFICIAL] https://developers.facebook.com/docs/instagram-platform/content-publishing
- The same page lists resumable upload from **a file on your computer** as a supported source for video. This may remove
  the need for a public URL. [OFFICIAL, page list] [VERIFY the endpoint and that Reels accepts it]
- The API allows 100 published posts per account in a rolling 24 hours. The carousel section of the same page says 50.
  Plan for 50. [OFFICIAL, conflicting]
- The usage endpoint is `GET /<IG_ID>/content_publishing_limit`. [OFFICIAL]
- Publishing permissions are named `instagram_content_publish` (Facebook or Unified Login) and
  `instagram_business_content_publish` (Instagram Login). [OFFICIAL] Check that the Meta configuration uses the name the
  connector expects.
- Advanced Access and App Review are needed when the app serves accounts it does not manage. Business verification may be
  needed first. A forum user reported more than ten days for verification. [SECONDARY] Meta forum and vendor guides;
  check Meta's App Review page [VERIFY].
- No native scheduling for Reels has been confirmed; third-party sources say containers expire after about 24 hours, so
  they should be created close to the publish time. [SECONDARY] https://www.postproxy.dev/how-to/schedule-instagram-reels/ [VERIFY]

**Options researched.**

| Option | Verdict |
|---|---|
| A. Resumable upload from a local file (no public URL) | Best if the Reels endpoint accepts it. Verify first |
| B. Public URL through a tunnel (localhost.run) | Works for tests; fragile; not for daily use |
| C. Public storage (Cloudflare R2 with a public bucket, or the hosted backend) | Reliable; costs and setup; depends on item 34 |
| D. Manual upload with a generated caption | Always works; fallback |

**Decision.** A first, verified with a test call. B for tests only. C when item 34 is decided. D always available.

**Steps.**
1. Create a Meta app (Business type). Add the Instagram product with Unified Login. Create the configuration and note its
   ID (`extra_fields.config_id`).
2. Set `META_APP_ID`, `META_APP_SECRET` and the configuration ID.
3. Connect a professional (Business or Creator) account. Confirm the permission on the consent screen.
4. **First live test publishes a public Reel.** The Instagram API has no private or draft Reel. Use a dedicated test account,
   not the main one.
5. Verify route A with one test. If it works, switch the connector to it and keep B only for development.
6. Request Advanced Access only if accounts the app does not manage must connect.

**Fallbacks.**
- Advanced Access refused: use Standard Access, which covers accounts that have a role in the app (check Meta's rules [VERIFY]).
- Route A refused for Reels: use C, or D.
- 50 or 100 posts a day reached: queue (rule 2); the usage endpoint shows the remaining count.
- Container status `ERROR`: show the reason from the API; do not retry the same container.
- Token invalid (code 190): mark the account "reconnect needed".

**Errors to prevent.** A public URL that is not reachable at attempt time (test the URL before creating the container). A
container that times out while polling (use a maximum wait, then a clear error). A duplicate publish after a restart
(store the container and media IDs; the connector already keeps `container_id`). Captions over 2,200 characters. Reels over
15 minutes (the limit is in the connector).

**Done when.** A test Reel on a dedicated test account publishes. The reconnect works after a token expiry. The daily count
is shown from the usage endpoint.

**Resources.**
- Content publishing [OFFICIAL]: https://developers.facebook.com/docs/instagram-platform/content-publishing
- IG user media reference [OFFICIAL]: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
- Meta App Review (search result) [VERIFY]: https://developers.facebook.com/documentation/instagram-platform/app-review
- Business verification discussion [SECONDARY]: https://communityforums.atmeta.com/discussions/Questions_Discussions/business-verification-in-review-10-days-%E2%80%94-blocking-app-review-submission/1372323

---

### 3.5 Item 26: No accounts are connected (Pending)

**Current state.** `GET /api/publish/accounts` is empty. Accounts are stored encrypted under `data/publish/accounts/`.

**Goal.** One connected, healthy account per platform, shown in the UI with its profile name.

**Steps.** After items 23 to 25 are done for a platform: connect from the Accounts page; check the profile name and status;
set the account's default privacy; add it to a category (item 22).

**Fallbacks.** Token expired: a "reconnect needed" banner, and no publish attempts for that account until it is fixed.
Account removed on the platform: `AuthError`; mark and stop.

**Done when.** One account per platform is connected and healthy after a restart.

---

### 3.6 Item 28: Auto-approval mode publishes without a person (Done)

**Status: done for the main path.** Automatic approval was removed. Categories are always `manual`, `AutoPublisher.on_rendered`
and its worker hook are gone, the category page no longer offers an auto mode, and an old `approval: "auto"` in
`categories.json` is migrated to `manual` (the original file is kept as `categories.backup-before-auto-removal.json`).
The publisher's own create-job route (`POST /api/publish/jobs`) skipped the approval check. It was removed, so posts are
created only through `POST /api/jobs/{id}/publish`.

**Goal.** No code path can publish a clip unless a person approved it.

**Options researched.**

| Option | Verdict |
|---|---|
| A. Remove `auto` from the code and the UI | Matches the rule exactly. Recommended |
| B. Keep it, off by default, with a typed confirmation and an audit entry per category | Possible if the owner wants it later |
| C. Keep it, but only for private drafts (never public) | A middle ground; still publishes without a person |

**Decision.** A, chosen by the owner (Section 6, decision 2).

**What was done (A).**
1. `categories.json` is backed up before the migration (rule 11).
2. On load, `approval: "auto"` becomes `manual`. `_clean` rejects any value other than `manual`.
3. `on_rendered` and its worker hook are removed, so rendering never approves or publishes.
4. `human_approved(clip)` in `catalog.py` requires `review.approved is True` and `approved_by == "person"`. The clip publish
   path uses it. `review_clip` sets `approved_by: "person"` when a person approves. Clips approved before this change have
   no `approved_by`, so they must be approved again before they can publish.
5. Tests in `backend/tests/test_catalog.py`: `auto` is refused, the old file is migrated, only a person's approval passes
   the check, and nothing posts until a person approves.

**Closed.** The route is removed from `publisher/api.py`, along with the models only it used. A test checks that
`POST /api/publish/jobs` now returns 405. Every publish path goes through `human_approved`.

**Fallbacks.** If the owner keeps option B: require a typed phrase, record who enabled it and when, and show a banner on
every job from that category.

**Errors to prevent.** An old `auto` value in a file (migration on load). A new code path that sets `approved` (the
invariant test catches it).

**Done when.** No code path sets `approved` to true without a human action. The tests prove it.

---

### 3.7 Item 29: Scheduling and spacing (Done in code, not tested live)

**Current state.** Publish jobs support `spacing_minutes` and `scheduled_at`. Nothing has been run live.

**Goal.** A set of clips posts at the planned times, even if the backend restarts.

**Facts.** Instagram: no native scheduling for Reels is confirmed (item 25). YouTube: a private video can carry a
`publishAt` time [VERIFY in the API reference]. TikTok: inbox drafts expire after 24 hours [SECONDARY].

**Steps.**
1. Store every planned time in UTC. Show local time in the UI.
2. Run a scheduler loop in the backend that wakes every minute and starts the publish jobs whose time has come.
3. For Instagram, create the container shortly before the planned time (a few minutes), then publish at the time.
4. For YouTube, upload as private with `publishAt` if the reference confirms it. Otherwise publish at the time.
5. For TikTok inbox, create the draft shortly before the time the owner will be free, and notify the owner.
6. Missed time (backend was down): publish on start if the time is within 2 hours; otherwise mark it missed and ask.

**Fallbacks.** Backend down at the planned time: item 34 is the real fix. Until then, the owner is told before scheduling.

**Errors to prevent.** Double posting after a restart (publish pairs are idempotent; stored IDs). Wrong time zone. Containers
created too early and expired (create them late).

**Done when.** Three clips with a 10-minute spacing post at T, T+10 and T+20 in a mock run, and at the right times on a test
account.

---

### 3.8 Item 30: Publishing reliability (Done in code, not live)

**Current state.** 43 publisher tests, mostly with mocked HTTP (`tests/publisher/`). Retry settings exist
(`PUBLISHER_MAX_ATTEMPTS`, `PUBLISHER_RETRY_BASE`). Nothing has been run against a live platform.

**Goal.** Every platform error has one behaviour; a failing platform never blocks the others.

**Steps.**
1. Map every platform error code to a class in rule 1 (`RateLimitError`, `AuthError`, `ContentError`, `TransientError`).
2. Add a circuit breaker per platform (rule 3).
3. Recorded fixtures: one success and each documented error per platform, from the platform's own docs.
4. A live checklist per platform (private test, token refresh, quota error, reconnect), run once before enabling it.
5. Show publish counts and the last error per platform on the Posts page.

**Fallbacks.** Platform paused: the queue is kept and the owner gets one message. Manual posting: download the clip and
the caption, which the app can generate.

**Errors to prevent.** Two refreshes at once for one account (one lock per account). Partial uploads counted as published.
Retrying a `ContentError` (it can never succeed).

**Done when.** The contract tests cover every documented error code, and each platform passes its live checklist.

---

### 3.9 Item 31: Sponsored content disclosure (Gap)

**Current state.** A campaign brief can ask for `#Ad`, but nothing checks it before posting. Only YouTube's `#Shorts` is
required in code.

**Goal.** A sponsored clip cannot be posted without a clear disclosure.

**Facts.** Instagram offers a "Paid partnership" label for business accounts with the brand tagged. [SECONDARY] A legal
analysis warns the label may not be enough on its own, and plain text disclosure is advised. [SECONDARY]
https://www.mondaq.com/unitedstates/social-media/1787954/tag-youre-liable-what-instagrams-new-affiliate-reels-mean-for-retailers
The FTC's own guidance must be read directly. [VERIFY] https://www.ftc.gov

**Steps.**
1. Add `sponsored` (yes or no) and `brand` (text) to the category and to the job.
2. When sponsored, the caption starts with a disclosure line (for example "Paid partnership with {brand}" and "#ad").
   Validate before publish.
3. For Instagram, set the branded-content tag through the API only if the reference confirms a field for it [VERIFY].
   Otherwise, show a checklist on the publish screen: "Turn on Paid partnership label".
4. Block the publish if the disclosure is missing.

**Fallbacks.** Sponsorship unknown: the owner must choose; the default is "not sponsored" and the checklist still shows.

**Errors to prevent.** Disclosure cut off by "more" (keep it in the first 125 characters). A disclosure in the hashtags only.

**Done when.** A sponsored test clip cannot publish without the disclosure. The checklist appears for Instagram.

---

### 3.10 Item 34: Production hosting (Pending)

**Current state.** The frontend is on Vercel and rewrites `/api` and `/media` to a Render backend (`vercel.json`). The
backend writes jobs, uploads, renders and encrypted tokens to its local disk (`CLIPPER_DATA`). The owner's PC runs the
backend during development.

**Goal.** A backend that is always reachable, keeps its data across restarts, and has the tools the pipeline needs
(ffmpeg, yt-dlp, Whisper on CPU).

**Facts that shape the design.**
- A free Render web service spins down after 15 minutes without traffic and takes about a minute to wake. [OFFICIAL]
  https://render.com/docs/free
- Each workspace gets 750 free instance hours per month, after which its free services are suspended. [OFFICIAL]
- Files on a free web service are lost on every redeploy, restart or spin-down. Paid services can attach a persistent
  disk. [OFFICIAL] https://render.com/docs/free
- **Consequence:** running the backend on a free Render service would lose jobs, uploads, renders and encrypted tokens.
  That is not acceptable for publishing.

**Options researched.**

| Option | Verdict |
|---|---|
| A. The owner's PC runs the backend, with a tunnel for the frontend | Private use only; the PC must stay on |
| B. Render paid instance with a persistent disk | Simplest; keeps the current deploy; costs money [VERIFY the price] |
| C. A small VPS with a disk (any provider) | Cheapest for always-on; more admin work [VERIFY prices] |
| D. Split: frontend on Vercel, backend on a VPS, media served from object storage (Cloudflare R2) for public URLs | Good for Instagram route C; more moving parts. R2 free tier 10 GB [SECONDARY, pricing figures conflict] |

**Decision.** The owner chooses (Section 6, decision 3). Recommended: B or C for an always-on backend with a disk.

**Steps.**
1. Choose the host and the plan.
2. Set `CLIPPER_DATA` to the persistent path.
3. Deploy with the existing `backend/Dockerfile`. Check that it installs ffmpeg and the Python dependencies (`[VERIFY]` the file).
4. Set the environment: LLM settings, platform keys, `APP_PASSWORD`, `CLIPPER_SECRET_KEY`, `CORS_ORIGINS`.
5. Back up the data folder on a schedule (rule 11).
6. Use the existing health route (`/api/health`) for monitoring.
7. Point `vercel.json` at the new backend URL.
8. Smoke test: upload a short video, run a 60-second job, publish a private test.

**Fallbacks.** Backend down: the frontend shows "backend offline" (already). Publishes wait in the queue. The owner's PC can
run the backend as a temporary replacement.

**Errors to prevent.** A cold start during a publish (use an always-on plan). Disk full (check free space at start, rule
7). Secrets committed to git (confirm `.gitignore` covers `.env` and the data folder; both are listed for the backend).
CORS errors after a domain change.

**Done when.** After a host restart, a queued job and an approved publish survive. A 24-hour soak test has no data loss.

**Resources.**
- Render free plan [OFFICIAL]: https://render.com/docs/free
- Cloudflare R2 one-page overview [OFFICIAL]: https://cf-assets.www.cloudflare.com/slt3lc6tev37/11IEu0J8L9rxxcqCW6m2hP/3e7e90d162f7414d914b1229fc97cae1/R2_one-pager.pdf
- Cloudflare R2 pricing summary [SECONDARY]: https://upstash.com/kb/cloudflare-r2-pricing-in-2026-storage-operations-and-real-monthly-bills

---

## 4. Smaller gaps and items that work today

### 4.1 Item 3: Batch intake does not apply branding or music (Done, gap)

**Current state.** A batch creates jobs with the batch options only. `BatchIn` has no `use_branding`, `music` or
`music_volume` fields, so batch clips get no logo, progress bar or music.

**Steps.** Add the three fields to `BatchIn`. Pass them into the batch options, with branding taken as a snapshot at
creation (the same rule as single jobs). Add the checkbox to the batch tab. Test: a two-line batch produces jobs with
`options.branding` set.

**Done when.** A batch of two links renders with the owner's logo and bar.

### 4.2 Item 27: The human approval gate (Done; keep it safe)

**Current state.** Publishing sends only approved clips. The publish button is disabled until something is approved
(`ClipCard` in `JobPage.tsx`), and `publish_approved` filters on `human_approved` (`catalog.py`). Approving a clip does
not post it: the person picks the accounts for that clip and publishes it. The publisher's create-job route was removed (3.6).

**Keep working.** Add the invariant test from item 28, step 6. Any new publish route must call the same check.

### 4.3 Items 2, 4, 6, 7, 9, 16, 18, 19, 20, 21, 32, 33 (Done, keep working)

| Item | What works | Keep it working with |
|---|---|---|
| 2 Upload from PC | Chunked upload, 20 GB limit, progress bar | A test with a 2 GB file |
| 4 Trends | YouTube trending research (not part of posting) | Quota check before each refresh |
| 6 Whisper for uploads | faster-whisper on CPU, int8 | The CPU fallback test |
| 7 Claude picks | Task file, selection validation | The mock-response test |
| 9 Action picks | Motion and sound for no-speech video | One gaming test video |
| 16 Branding | Logo, progress bar, per-clip override, rerender | The render test in `tests/` and the UI check |
| 18 Render | 9:16 encode, captions, cache keys | The cache-key test |
| 19 Clip ends | Sentence ends only; tail stops before the next line | The `tail_end` test |
| 20 Review | Approve, reject, notes, trim | The trim tests |
| 21 Rerender | Retry forces a render; one clip re-renders | The retry test |
| 32 Posts tracker | Status and URLs per clip | Empty today; test with a mock publish |
| 33 Platform captions and hashtags | Per-platform limits, optional LLM captions | The variants tests |

---

## 5. Error catalogue

Every error the pipeline can raise, its class (rule 1), what the app does, and the fallback. Items marked "already" are
built; the others are part of the plan above.

| Error (as shown or logged) | Class | Cause | Behaviour | Fallback |
|---|---|---|---|---|
| "Sign in to confirm you're not a bot" (yt-dlp) | Blocked | YouTube blocks the request | Stop; no retry; cooldown 6 h (planned) | Upload the file (item 12) |
| YouTube `quotaExceeded` (403) | RateLimit | Daily units or 100 uploads reached | Queue until reset | Manual upload |
| YouTube `invalid_grant` | Auth | Refresh token expired (Testing: 7 days) | "Reconnect needed" | Reconnect (item 23) |
| TikTok "app not audited" / SELF_ONLY | Content | Unaudited app | Post forced to private or inbox (already) | Audit (item 24) |
| TikTok `spam_risk_too_many_posts` | RateLimit | Daily cap for the user | Queue to next day | None needed |
| Instagram code 190 | Auth | Token invalid | "Reconnect needed" | Reconnect |
| Instagram codes 4, 17, 32, 613 | RateLimit | App or user limit | Wait 60 s, then queue (already) | Queue |
| Instagram container `ERROR` or `EXPIRED` | Content | Processing failed or container too old | New container, at most two tries; show the reason | Manual upload (D) |
| Instagram cannot fetch `video_url` | Transient or Config | Public URL down or not public | Check the URL before the container (planned) | Route A (item 25) |
| LLM 429 | RateLimit | Free-tier limit | Backoff; offer "Run with Claude instead" (planned) | Claude mode, Ollama |
| LLM "No API key" | Config | Key not set | Stop before any work (already) | Claude mode |
| ffmpeg not found | Config | Not installed or not on PATH | Stop with the install message; imageio-ffmpeg used if present (already) | Install ffmpeg |
| Whisper CUDA libraries missing | Config | GPU libraries absent | CPU int8 (already) | None needed |
| "This video has no audio track" | Content | Silent source | Picture-only render with silent audio (already) | None needed |
| "Music track ... is not in the library" | Config | File deleted after selection | Job refused at creation (already) | Choose another track |
| Overlapping or invalid pick | Content | Model output | Pick dropped; others kept (already) | Top-up from scores |
| ffmpeg failed while rendering | Content | Bad input or filter | Clip fails with the last 800 characters of ffmpeg output (already) | Re-download and render |
| Disk full | Config | No space on the data drive | Stop writes; show free space (planned) | Free space or move `CLIPPER_DATA` |
| Platform circuit open | Rate or Auth | Repeated failures | Pause that platform; queue kept (planned) | Owner decides |

---

## 6. Decisions the owner must make

Each decision blocks one or more items. Recommended answers are marked. Nothing in this list is implemented until the
owner answers.

| # | Decision | Options | Recommended | Blocks |
|---|---|---|---|---|
| 1 | Policy for downloading YouTube media | (a) Uploaded files only; (b) only videos the owner has rights to; (c) owner-initiated cookies setting, off by default | (a) plus (b); (c) only after legal review | 2.1, 2.4 |
| 2 | Auto-approval mode | (a) Remove it; (b) keep, off by default, typed confirmation | (a). Answered: done (3.6) | 3.6, 3.1 |
| 3 | Where the backend runs | (a) Own PC; (b) Render paid with disk; (c) VPS with disk; (d) split with object storage | (a). Answered: own PC for the MVP | 3.10, 3.7 (hosting work deferred) |
| 4 | LLM provider for picks | Claude mode only; add Gemini key; add Ollama | Claude mode plus Gemini key | 2.2 |
| 5 | YouTube app status | (a) Testing with the owner as test user; (b) request verification now | (a) now, (b) when others connect | 3.2 |
| 6 | Hugging Face account for speaker detection | Create a free account and accept the model terms | Yes | 2.5, 2.4 |
| 7 | Instagram test account | A dedicated professional account for test posts | Yes; the first test posts publicly | 3.4 |
| 8 | Music source and budget | YouTube Audio Library and Pixabay only; or a paid library | Free sources first | 2.8 |
| 9 | The "actionable" takeaway | (a) Hard rule: a clip without one is rejected; (b) preference: weighted in scoring | (b), then review after calibration | 2.3 |

---

## 7. Build order

Items depend on each other. This order avoids rework and keeps every step testable without touching a live platform
until the step before it passed.

| Phase | Work | Items | Needs from owner | Done when |
|---|---|---|---|---|
| 0 | Decisions | Section 6 | Answers 1 to 8 | Each decision is recorded in this file |
| 1 | Safety with no platform calls | 28 (invariant and migration), 3 (batch options), error classes (rule 1), start-up self-check (rule 7), backups (rule 11), sponsored checklist (31, first step) | None | All tests pass; the invariant test proves no auto publish |
| 2 | Input robustness | 12 (blocked state, cooldown, upload route), 8 (LLM key and fallback), 2.4 (transcript quality badge) | Decision 1, decision 4 | A blocked job finishes from a file; a forced 429 offers Claude mode |
| 3 | Platform setup, private first | 23 (YouTube private test), 24 (TikTok inbox), 25 (Instagram route A test), 22 (category), 26 (accounts) | Decisions 5, 7; developer accounts | One private or inbox item per platform, reconnect after a restart |
| 4 | Operations | 29 (scheduler), 30 (contract tests, circuit breaker, live checklists), 31 (sponsored gate complete) | Phase 3 done | Three scheduled clips post on time; each platform's checklist passed |
| 5 | Quality | 11 (actionable, context check, calibration), 13 (two-person layout), 14 (caption presets), 15 (OCR test), 17 (music library) | Decisions 6, 8 | Measured numbers recorded for each item |
| 6 | Hosting | 34 (host, data, backups, health, Vercel target) | Decision 3 | 24-hour soak test with no data loss |

Phase 6 can start in parallel after Phase 1. Unattended scheduling should not run until Phase 6 is done.

**Whole-pipeline acceptance test (run once after Phase 4, and again after Phase 6).**
1. Paste a link (or upload a file). The job runs to "Ready to review".
2. Approve one clip as a person. Check that nothing else was approved.
3. Publish to each connected platform as private or inbox. Check each platform's own app or Studio.
4. Check the Posts page and the job log.
5. Reject one clip. Check that it never reaches a platform.
6. Restart the backend during step 3. Check that nothing was posted twice.

---

## 8. Sources

Grouped by topic. [OFFICIAL] pages were read during this research; [SECONDARY] are vendor or third-party pages; [VERIFY]
were not confirmed. Pages that failed during research are listed separately, so they are re-checked before use.

**YouTube**
- Quota costs and default allocations [OFFICIAL]: https://developers.google.com/youtube/v3/determine_quota_cost
- Music eligibility for Shorts and the YouTube Audio Library [OFFICIAL]: https://support.google.com/youtube/answer/13486873
- OAuth verification and the 100-user limit [SECONDARY, found by search]: https://support.google.com/cloud/answer/7454865
- Pricing and quota overview [SECONDARY]: https://www.blotato.com/blog/youtube-api-pricing

**TikTok**
- Direct Post reference [OFFICIAL snippet; page returned 503 when fetched]: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
- Photo post reference, inbox mode [OFFICIAL snippet]: https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
- Postiz provider notes [SECONDARY]: https://docs.postiz.com/self-host/providers/tiktok

**Instagram and Meta**
- Content publishing [OFFICIAL]: https://developers.facebook.com/docs/instagram-platform/content-publishing
- IG user media reference [OFFICIAL]: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
- App Review (found by search) [VERIFY]: https://developers.facebook.com/documentation/instagram-platform/app-review
- Business verification forum thread [SECONDARY]: https://communityforums.atmeta.com/discussions/Questions_Discussions/business-verification-in-review-10-days-%E2%80%94-blocking-app-review-submission/1372323
- Scheduling notes [SECONDARY]: https://www.postproxy.dev/how-to/schedule-instagram-reels/

**Hosting and storage**
- Render free plan [OFFICIAL]: https://render.com/docs/free
- Cloudflare R2 overview [OFFICIAL]: https://cf-assets.www.cloudflare.com/slt3lc6tev37/11IEu0J8L9rxxcqCW6m2hP/3e7e90d162f7414d914b1229fc97cae1/R2_one-pager.pdf
- Cloudflare R2 pricing summary [SECONDARY]: https://upstash.com/kb/cloudflare-r2-pricing-in-2026-storage-operations-and-real-monthly-bills

**LLM providers**
- Google AI Studio rate limits [OFFICIAL]: https://aistudio.google.com/docs/rate-limits
- Per-model free-tier table [VERIFY]: https://aistudio.google.com/rate-limit
- LLM judge calibration [SECONDARY]: https://mlflow.org/articles/llm-judge-calibration/
- Pointwise and pairwise judging [SECONDARY]: https://arxiv.org/html/2602.02219v1
- G-Eval paper [VERIFY]: arXiv 2303.16634

**Speech, speakers and video**
- faster-whisper, MIT licence [OFFICIAL]: https://github.com/SYSTRAN/faster-whisper
- pyannote.audio, MIT code; model terms on Hugging Face [OFFICIAL]: https://github.com/pyannote/pyannote-audio
- WhisperX [VERIFY licence]: https://github.com/NorSC/whisperX
- TalkNet ASD [SECONDARY licence; VERIFY]: https://github.com/TaoRuijie/TalkNet_ASD
- LR-ASD [VERIFY licence]: https://github.com/airmeet/LR-ASD
- Light-ASD [VERIFY licence]: https://github.com/Junhua-Liao/Light-ASD

**Music and disclosure**
- Pixabay licence [OFFICIAL]: https://pixabay.com/service/license/
- Paid partnership and the legal view [SECONDARY]: https://www.mondaq.com/unitedstates/social-media/1787954/tag-youre-liable-what-instagrams-new-affiliate-reels-mean-for-retailers
- FTC guidance [VERIFY]: https://www.ftc.gov

**yt-dlp**
- FAQ, linked from the bot-check error text [OFFICIAL]: https://github.com/yt-dlp/yt-dlp/wiki/FAQ

**Pages to re-check before use (failed or redirected during research)**
- TikTok Direct Post reference (503).
- YouTube API audit guide (404): find the current URL on developers.google.com.
- Gemini rate-limit page (redirected to AI Studio; per-model table not read).
- Meta App Review submission guide and Instagram Login scope names.

---

## 9. How to keep this document current

- Each item has a **Done when** line. An item is Done only when that line is true on the owner's machine, and the
  status in the audit table is updated with the date.
- When a platform changes a limit or a rule, change the number here first, then the connector, then its test.
- Facts marked [SECONDARY] or [VERIFY] are checked again before a phase starts.
- Add a new item here before writing code for it.
