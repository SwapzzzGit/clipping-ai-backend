# Timestamped review marks: trim a clip and re-render only that clip

Date: 2026-10-31 · Status: approved design, awaiting spec review

## Goal

While reviewing a rendered clip in Clipper Studio, the reviewer pauses the video and clicks
**Start here** or **End here**. Each click is saved as a timestamped mark on that clip. Clicking
**Re-render** moves the clip's in/out points to the marks and rebuilds only that clip, without
downloading anything again and without touching the other clips.

Borrowed from makerzz.space ("leave a note at the exact second, only that part is redone"),
scoped down to trimming.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Which notes change the video in v1 | Trim start / end only |
| How the reviewer enters them | Playhead buttons, no typing, no LLM |
| When the re-render runs | Marks stay pending until the reviewer clicks **Re-render**; **Reset** clears them |
| How the re-render runs | Reuse the normal job run (approach A); per-clip caches make the other clips no-ops |

Out of scope for v1: extending a clip past its current start/end, cutting a span out of the
middle, caption text fixes, LLM/Claude interpretation of free-text notes. The existing free-text
`review.notes` field is unchanged.

## Current behaviour this builds on

- `job["picks"]` is the list of chosen clips (`id`, `start_sec`, `end_sec`, `title`, `hook_line`,
  `text`, ...). `job["clips"]` is the manifest view, including `review: {approved, notes}`.
- `download_clip` saves `[start - 0.5 s, end + 0.5 s]` as `work/<vid>/raw/clip_<id>_<startms>_<endms>.mp4`
  and reuses that exact file when it exists.
- Face analysis is cached per raw file stem; `render_clips` skips a clip whose `.render.json` key
  (`raw`, `start_sec`, `end_sec`, `render`) is unchanged.
- `render_clip` trims the raw file to `[start - 0.10 s, end + 0.35 s]`, clamped to the raw file.
- The worker's `_run` re-reads cached transcript/info, skips selection when `picks` is set, then
  downloads, renders, rewrites the manifest and carries each clip's `review` over.

## Design

### 1. Data

**Render records where each output begins.** `render_clip` returns a new field
`source_t0 = raw.offset_sec + trim_start` (source-video seconds at the rendered clip's first
frame). `build_manifest` copies it into each clip as `source_t0`. Clips rendered before this change
have no `source_t0`; for them the backend falls back to `start_sec - 0.10`.

**Marks live in the review.** `review` becomes:

```json
{
  "approved": null,
  "notes": "",
  "marks": [
    {"kind": "start", "t": 4.2, "source_sec": 1834.6, "at": "2026-10-31T14:02:11+00:00"}
  ],
  "trims": [
    {"from": [1830.5, 1872.1], "to": [1834.6, 1866.0], "at": "2026-10-31T14:03:40+00:00"}
  ]
}
```

- `t` is the player time the reviewer clicked at (seconds into the rendered clip).
- `source_sec` is the snapped source-video time (see below).
- Only the most recent `start` mark and the most recent `end` mark are pending; older marks are
  kept so the reviewer sees the history.
- `trims` is a log of applied re-renders (old and new range). On re-render the applied marks are
  cleared from `marks`.
- `marks` and `trims` default to `[]`; existing jobs without them load fine.

**Snapping.** A new pure function in `clipper/candidates.py`:

```python
def snap_mark(t: float, kind: str, source_t0: float, segments: list[dict],
              lo: float, hi: float) -> float
```

- `source = source_t0 + t`.
- `kind == "start"`: the start of the first word whose end is `> source + 0.15` (the word under the
  playhead is kept unless only its last 0.15 s remains).
- `kind == "end"`: the end of the last word whose start is `< source - 0.15` (the word under the
  playhead is kept unless only its first 0.15 s has played).
- Result is clamped to `[lo, hi]` (the clip's current `start_sec` / `end_sec`), so a mark can only
  shrink the clip.
- Without word timings (manual subs), segments act as pseudo-words, as in `boundaries()`.

### 2. API (backend/app/main.py)

| Endpoint | Body | Behaviour |
|---|---|---|
| `POST /api/jobs/{id}/clips/{clip}/marks` | `{kind: "start" \| "end", t: float}` | Snaps and appends a mark; returns the job. 404 for unknown clip, 422 for `t` outside `[0, duration_sec]`. |
| `DELETE /api/jobs/{id}/clips/{clip}/marks` | none | Clears pending `marks` (Reset). |
| `POST /api/jobs/{id}/clips/{clip}/rerender` | none | Applies pending marks (see below). |

`rerender`:

1. 409 if the job's status is `queued`, `running`, `cancelling` or `awaiting_claude`.
2. 422 if the clip has no pending marks.
3. New range: `start = latest start mark's source_sec` or the current `start_sec`; `end` likewise.
   422 if `end - start < 3.0` s or `start >= end`.
4. Update that clip in `job["picks"]`: `start_sec`, `end_sec`, and `text` (rebuilt from the
   transcript words in the new range). `title` and `hook_line` stay unchanged.
5. In `job["clips"]`: append to `review.trims`, clear `review.marks`, set `review.approved = None`
   (the video changed), keep `review.notes`.
6. Set `status = "queued"`, `stage = "download"`, `error = None`, `finished_at = None`, and log
   "Re-rendering clip N: a-b -> c-d".

The worker then runs its normal `_run`. Transcript, info, the other clips' raw files, analysis and
renders are all cached, so only the edited clip is encoded again.

### 3. Download reuse (clipper/download.py)

Before downloading, `download_clip` looks for an existing valid raw file of the same clip id whose
range (parsed from `clip_<id>_<startms>_<endms>.mp4`) covers `[start_sec - 0.1, end_sec + 0.35]`,
the span `render_clip` needs. If one exists it is returned (with its own `offset_sec` / `end_sec`)
instead of downloading. Because marks only shrink a clip, every re-render reuses the original raw
file, and its cached face analysis comes along with it (analysis is keyed by raw stem).

`render_clips` already calls `render_clip`, which handles any `offset_sec`, so it needs no change
apart from passing `source_t0` through.

### 4. Worker (backend/app/worker.py)

No new stages. One change: when carrying reviews from the old `job["clips"]` into the new
manifest, keep the whole `review` object (`approved`, `notes`, `marks`, `trims`). It already does
this through `reviews.get(c["id"])`; this only needs the default review in `build_manifest` to
include `marks: []` and `trims: []`.

### 5. UI (frontend/src/pages/JobPage.tsx, frontend/src/api.ts)

On each rendered clip card:

- **Start here** / **End here** buttons, enabled when the video has loaded. They post
  `{kind, t: video.currentTime}`.
- A thin bar under the video showing the full rendered length and the pending trim (shaded parts
  are what will be cut). Clicking a mark's tick seeks the video to it.
- A list of pending marks ("0:04 start here", "0:41 end here") and of applied trims.
- **Re-render** (disabled with no pending marks or while the job is busy) and **Reset**.
- While the job is `queued`/`running` after a re-render, the card shows "Re-rendering…"; the
  video URL gets a cache-busting `?v=<finished_at>` so the new file loads (the rerender endpoint sets a transient
  `rerendering: true` on the clip, which the rebuilt manifest drops).

`api.ts` gains `addMark`, `clearMarks`, `rerender`, and the `Review` type gains
`marks` and `trims`.

## Error handling

- Download of a covering raw fails or is missing: falls through to the normal download, which may
  fail like any download; the job fails with the existing error message and can be retried.
- Render fails for the edited clip: that clip shows `failed` with the ffmpeg error, as today; the
  others stay `ok`. The reviewer can Reset and retry, or use the existing Retry button.
- Marks posted while the job is running are accepted (they only change `review`), but Re-render
  waits until the job is idle (409).

## Testing

Unit (tests/):

- `snap_mark`: snaps to word start/end, respects the 0.15 s tolerance, clamps to `[lo, hi]`,
  works with pseudo-words when there are no word timings.
- `download_clip` covering-raw reuse: a valid raw covering the range is returned without running
  yt-dlp (subprocess patched); a non-covering one triggers a download.
- `render_clip` returns `source_t0`.

API (backend/tests/test_api.py):

- Adding a start and end mark stores snapped `source_sec` values inside the clip.
- `rerender` updates `picks` start/end/text, clears marks, logs the trim, resets `approved`,
  queues the job at `download`.
- `rerender` returns 409 while the job is running, 422 with no marks, 422 when the result is under 3 s.
- `DELETE …/marks` clears pending marks.

Manual: run one real job, trim a clip, confirm in the log that the other clips report "already
rendered" and no download happens, and that the new MP4 starts and ends on the marked words.
