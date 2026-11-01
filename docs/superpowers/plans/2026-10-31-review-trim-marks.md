# Review Trim Marks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reviewers mark Start/End at the video playhead on a rendered clip, then Re-render that one clip trimmed to the marks, with no new download.

**Architecture:** Marks are stored in each clip's `review`. A pure helper module turns marks into a new range and edits the job's `picks`. Re-render just re-queues the job at the `download` stage: the existing per-clip caches skip every other clip, and a new "covering raw" lookup in `download_clip` reuses the original download.

**Tech Stack:** Python 3.12, FastAPI, pytest, ffmpeg/yt-dlp (unchanged); React 18 + TypeScript + Vite.

**Spec:** `docs/superpowers/specs/2026-10-31-review-trim-marks-design.md`

## Global Constraints

- Marks can only shrink a clip: snapped times are clamped to the clip's current `[start_sec, end_sec]`.
- Snap tolerance: `0.15` s. Minimum trimmed length: `3.0` s.
- Render trims `[start - 0.10, end + 0.35]` (`HEAD_ROOM`, `TAIL_ROOM` in `clipper/render.py`); a covering raw must contain that span.
- Fallback when a clip has no `source_t0`: `start_sec - 0.10`.
- Re-render returns 409 when job status is `queued`, `running`, `cancelling` or `awaiting_claude`.
- Re-render resets `review.approved` to `null`, keeps `review.notes`, clears `review.marks`, appends to `review.trims`.
- `title` and `hook_line` never change on a trim; `text` (pick) is rebuilt from the new range.
- Run Python tests with `.venv/Scripts/python -m pytest tests backend/tests -q` from the repo root.

## Review Focus

1. Old jobs rendered before this change (no `source_t0`, no `marks`/`trims` keys): adding a mark must work using the fallback, not KeyError. → Task 4 test `test_mark_on_clip_without_source_t0`.
2. Playhead at `t = 0` or at the very end: snaps to the clip's own start/end instead of failing; `t` beyond `duration_sec` is 422. → Task 4 test `test_mark_bounds`.
3. A start mark placed after the end mark (or both inside one word): Re-render must refuse with 422, not render a negative clip. → Task 4 test `test_rerender_rejects_inverted_or_short`.
4. Same filename after re-render: the browser keeps showing the old cached MP4 unless the URL changes. → Task 5 step (cache-busting `?v=`), checked manually in Task 6.
5. Only an end mark (no start): start stays at the current `start_sec`. → Task 4 test `test_rerender_with_end_mark_only`.

---

### Task 1: Snapping and transcript-range helpers

**Files:**
- Modify: `clipper/candidates.py` (add after `snap`)
- Test: `tests/test_candidates.py`

**Interfaces:**
- Produces:
  - `snap_mark(source: float, kind: str, segments: list[dict], lo: float, hi: float) -> float` (`kind` is `"start"` or `"end"`)
  - `words_text(segments: list[dict], start: float, end: float) -> str`

- [ ] **Step 1: Write the failing tests** (use `make_segments` from `tests/conftest.py`; with 0.4 s words and 0.1 s gaps, `make_segments(["a b c d.", "e f g h."])` gives words a=[0,0.4] b=[0.4,0.8] c=[0.8,1.2] d=[1.2,1.6] e=[1.7,2.1] f=[2.1,2.5] g=[2.5,2.9] h=[2.9,3.3])

```python
def test_snap_mark_start_keeps_word_under_playhead():
    segs = make_segments(["a b c d.", "e f g h."])
    assert snap_mark(0.9, "start", segs, 0, 3.3) == 0.8      # inside c -> c.start
    assert snap_mark(1.1, "start", segs, 0, 3.3) == 1.2      # last 0.15 s of c -> next word d

def test_snap_mark_end_keeps_finished_word():
    segs = make_segments(["a b c d.", "e f g h."])
    assert snap_mark(1.0, "end", segs, 0, 3.3) == 1.2        # inside c -> c.end
    assert snap_mark(0.85, "end", segs, 0, 3.3) == 0.8       # first 0.15 s of c -> b.end

def test_snap_mark_clamps_to_clip():
    segs = make_segments(["a b c d.", "e f g h."])
    assert snap_mark(0.0, "start", segs, 0.8, 2.9) == 0.8
    assert snap_mark(9.0, "end", segs, 0.8, 2.9) == 2.9

def test_snap_mark_without_word_timings_uses_segments():
    segs = [{"start_sec": 0, "end_sec": 2, "text": "x", "words": []},
            {"start_sec": 2.5, "end_sec": 4, "text": "y", "words": []}]
    assert snap_mark(2.6, "start", segs, 0, 4) == 2.5

def test_words_text_covers_range():
    segs = make_segments(["a b c d.", "e f g h."])
    assert words_text(segs, 0.8, 2.5) == "c d. e f"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_candidates.py -q -k "snap_mark or words_text"`
Expected: FAIL with `ImportError: cannot import name 'snap_mark'`

- [ ] **Step 3: Implement both functions in `clipper/candidates.py`**

Build the word list the same way `boundaries()` does (segments become pseudo-words when there are no word timings; factor that into a private `_words(segments)` and use it in `boundaries()` too). Rules:
- start: the first word with `end > source + 0.15`; result is its `start`; none found → `hi`.
- end: the last word with `start < source - 0.15`; result is its `end`; none found → `lo`.
- clamp to `[lo, hi]`.
- `words_text`: join `text` of words with `start >= start - 0.01` and `end <= end + 0.01`, separated by single spaces.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_candidates.py -q`
Expected: all PASS (existing `boundaries` tests still pass after the `_words` refactor)

- [ ] **Step 5: Commit**

```bash
git add clipper/candidates.py tests/test_candidates.py
git commit -m "feat: snap review marks to word boundaries"
```

---

### Task 2: Record `source_t0` and default marks/trims in the manifest

**Files:**
- Modify: `clipper/render.py` (`render_clip`)
- Modify: `clipper/pipeline.py` (`build_manifest`)
- Test: `tests/test_render_parts.py`

**Interfaces:**
- Produces:
  - `trim_window(raw: dict, clip: dict, duration: float) -> tuple[float, float]` in `clipper/render.py`, returning `(trim_start, trim_end)` in raw-file seconds (the two lines currently inline in `render_clip`).
  - `render_clip(...)` result gains `"source_t0": round(raw["offset_sec"] + trim_start, 3)`.
  - Each manifest clip gains `"source_t0"` (from the render result, `None` when absent) and its default `review` becomes `{"approved": None, "notes": "", "marks": [], "trims": []}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_trim_window_adds_head_and_tail_room_and_clamps():
    raw = {"offset_sec": 99.5}
    assert trim_window(raw, {"start_sec": 100.0, "end_sec": 130.0}, 31.0) == pytest.approx((0.4, 30.85))
    assert trim_window({"offset_sec": 100.0}, {"start_sec": 100.0, "end_sec": 130.0}, 30.2) == pytest.approx((0.0, 30.2))

def test_manifest_carries_source_t0_and_empty_marks():
    # build_manifest with one clip and results {1: {"status": "ok", "file": "01_x.mp4", "source_t0": 99.9}}
    ...
    assert m["clips"][0]["source_t0"] == 99.9
    assert m["clips"][0]["review"] == {"approved": None, "notes": "", "marks": [], "trims": []}
```

For the manifest test, build minimal inputs: `info={"id": "vid", "title": "t"}`, `transcript={"source": "auto", "lang": "en", "has_word_timings": True, "heatmap": []}`, one clip dict with every key `build_manifest` reads (`id, title, hook_line, reason, score, score_breakdown, heatmap, selected_by, start_sec, end_sec, text`), `cfg` fixture, `llm={"model": "m", "usage": None}`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_render_parts.py -q`
Expected: FAIL with `ImportError: cannot import name 'trim_window'`

- [ ] **Step 3: Implement** — extract `trim_window`, use it in `render_clip`, add `source_t0` to its return dict; in `build_manifest` add `"source_t0": r.get("source_t0")` and the four-key default review.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python -m pytest tests -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add clipper/render.py clipper/pipeline.py tests/test_render_parts.py
git commit -m "feat: record where each rendered clip starts in the source"
```

---

### Task 3: Reuse a raw download that covers the new range

**Files:**
- Modify: `clipper/download.py` (`download_clip`)
- Test: `tests/test_download.py` (create)

**Interfaces:**
- Consumes: `HEAD_ROOM`, `TAIL_ROOM` from `clipper/render.py`.
- Produces: `find_covering_raw(raw_dir: Path, clip: dict, ffmpeg: str) -> dict | None` returning `{"path", "offset_sec", "end_sec"}` for a valid `clip_<id>_<startms>_<endms>.mp4` whose range contains `[start_sec - HEAD_ROOM, end_sec + TAIL_ROOM]`, else `None`. `download_clip` calls it after the exact-name check and before running yt-dlp.

- [ ] **Step 1: Write the failing tests** (create empty files in `tmp_path / "raw"`; `monkeypatch` `clipper.download.is_valid_video` to `lambda p, f, min_sec=1.0: p.is_file()` and `clipper.download.subprocess.run` to a function that records calls and raises `AssertionError("downloaded")`)

```python
def test_trimmed_clip_reuses_covering_raw(tmp_path, monkeypatch, cfg):
    raw = tmp_path / "raw"; raw.mkdir()
    (raw / "clip_01_99500_130500.mp4").write_bytes(b"x")
    r = download_clip("u", {"id": 1, "start_sec": 104.0, "end_sec": 120.0}, raw, cfg, "ffmpeg", 600)
    assert r == {"path": str(raw / "clip_01_99500_130500.mp4"), "offset_sec": 99.5, "end_sec": 130.5}

def test_other_clip_or_uncovered_range_is_not_reused(tmp_path, monkeypatch):
    raw = tmp_path / "raw"; raw.mkdir()
    (raw / "clip_02_99500_130500.mp4").write_bytes(b"x")
    assert find_covering_raw(raw, {"id": 1, "start_sec": 104.0, "end_sec": 120.0}, "ffmpeg") is None
    (raw / "clip_01_99500_130500.mp4").write_bytes(b"x")
    assert find_covering_raw(raw, {"id": 1, "start_sec": 99.5, "end_sec": 120.0}, "ffmpeg") is None   # needs 99.4
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_download.py -q`
Expected: FAIL with `ImportError: cannot import name 'find_covering_raw'`

- [ ] **Step 3: Implement `find_covering_raw` and call it from `download_clip`**; parse names with `re.fullmatch(r"clip_(\d+)_(\d+)_(\d+)\.mp4", name)`; log `"clip %d: reusing raw download %s"`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python -m pytest tests -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add clipper/download.py tests/test_download.py
git commit -m "feat: reuse an existing raw download that covers a trimmed clip"
```

---

### Task 4: Marks and re-render API

**Files:**
- Create: `backend/app/trims.py`
- Modify: `backend/app/main.py` (three endpoints next to `review_clip`)
- Test: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `snap_mark`, `words_text` (Task 1); manifest `source_t0` and `review.marks/trims` (Task 2).
- Produces in `backend/app/trims.py`:
  - `class TrimError(Exception)` with attribute `status: int` (409 or 422 or 404).
  - `add_mark(job: dict, clip_id: int, kind: str, t: float, segments: list[dict]) -> dict` — mutates the clip's review (creating `marks`/`trims` lists if missing), returns the mark `{kind, t, source_sec, at}`.
  - `apply_trim(job: dict, clip_id: int, segments: list[dict]) -> tuple[tuple[float, float], tuple[float, float]]` — validates, mutates `job["picks"]` and `job["clips"]` per the spec, sets `clip["rerendering"] = True` on the manifest clip (the worker's rebuilt manifest drops it naturally; it only carries `review` over), returns `(old_range, new_range)`.
  - `BUSY = {"queued", "running", "cancelling", "awaiting_claude"}`.
- HTTP: `POST /api/jobs/{job_id}/clips/{clip_id}/marks` body `MarkIn{kind: Literal["start","end"], t: float}`; `DELETE` same path → 200 with job; `POST /api/jobs/{job_id}/clips/{clip_id}/rerender`. All return the job. Segments come from `WORK_DIR / job["video"]["id"] / "transcript.json"`. All mutations go through `store.mutate`; `TrimError` becomes `HTTPException(e.status, str(e))`. Rerender also sets `status="queued", stage="download", error=None, finished_at=None` and logs `f"Re-rendering clip {id}: {mmss(a)}-{mmss(b)} -> {mmss(c)}-{mmss(d)}"`.

- [ ] **Step 1: Write the failing tests.** Add a helper `done_job_with_clip(vid)` that writes `make_transcript()` to `WORK_DIR/vid/transcript.json` and creates a `done` job with `video={"id": vid}`, one pick `{"id": 1, "start_sec": 10.0, "end_sec": 40.0, "title": "T", "hook_line": "H", "text": "..."}` and one clip `{"id": 1, "start_sec": 10.0, "end_sec": 40.0, "duration_sec": 30.45, "source_t0": 9.9, "review": {"approved": True, "notes": "keep", "marks": [], "trims": []}}`. Tests:

```python
def test_marks_are_snapped_and_stored():          # POST start t=5.0, end t=20.0 -> two marks, source_sec on word edges in [10, 40]
def test_mark_on_clip_without_source_t0():        # clip lacks source_t0, marks/trims keys -> 200, uses start_sec - 0.10
def test_mark_bounds():                           # t=0 start -> source_sec == 10.0; t=99 -> 422; unknown clip -> 404
def test_clear_marks():                           # DELETE -> review.marks == []
def test_rerender_updates_pick_and_queues():      # picks[0] start/end == marks, text rebuilt, review approved None,
                                                  # notes "keep", marks [], len(trims) == 1, status queued, stage download,
                                                  # clips[0]["rerendering"] is True
def test_rerender_with_end_mark_only():           # start_sec stays 10.0
def test_rerender_rejects_inverted_or_short():    # start mark after end mark -> 422; range < 3.0 s -> 422; no marks -> 422
def test_rerender_while_busy_is_409():            # status running -> 409
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest backend/tests -q`
Expected: new tests FAIL with 404/405 (routes missing)

- [ ] **Step 3: Implement `backend/app/trims.py` and the three routes.** Mark time to source: `source = (clip.get("source_t0") or clip["start_sec"] - 0.10) + t`; validate `0 <= t <= clip["duration_sec"]` (422). New range = latest start mark's `source_sec` (else `start_sec`) and latest end mark's (else `end_sec`).

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python -m pytest tests backend/tests -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/trims.py backend/app/main.py backend/tests/test_api.py
git commit -m "feat: review marks and single-clip re-render API"
```

---

### Task 5: Trim controls on the clip card

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/components/TrimControls.tsx`
- Modify: `frontend/src/pages/JobPage.tsx` (`ClipCard`)
- Modify: the stylesheet that defines `.clip` / `.review` (find with `grep -rn "\.review" frontend/src`)

**Interfaces:**
- Consumes: Task 4 HTTP routes.
- Produces in `api.ts`: `Mark {kind: "start" | "end"; t: number; source_sec: number; at: string}`, `Trim {from: [number, number]; to: [number, number]; at: string}`; `Review` gains `marks?: Mark[]; trims?: Trim[]`; `Clip` gains `source_t0?: number | null`; `api.addMark(id, clipId, kind, t)`, `api.clearMarks(id, clipId)`, `api.rerender(id, clipId)`, each `Promise<Job>`. `media(job, file)` appends `?v=${encodeURIComponent(job.finished_at ?? "")}` (Review Focus 4; not `updated_at`, which changes on every log line and would reload the video during a run). `Clip` also gains `rerendering?: boolean`.
- `TrimControls({ job, clip, videoRef, onSaved })`: Start here / End here (disabled until `videoRef.current?.readyState >= 1`), a bar showing `duration_sec` with shaded cut regions and clickable ticks (click → `videoRef.current.currentTime = mark.t`), pending marks as "0:04 start here" lines using `clock`, applied trims as "0:10–0:40 → 0:14–0:36", Re-render (disabled when no pending marks or `isActive(job)`) and Reset. While `isActive(job) && clip.rerendering`, show "Re-rendering…" in place of the buttons.

- [ ] **Step 1: Add the types and API calls to `api.ts`.**
- [ ] **Step 2: Create `TrimControls.tsx`; render it in `ClipCard` below the video when `clip.file` exists, passing a `useRef<HTMLVideoElement>` attached to the `<video>`.**
- [ ] **Step 3: Type-check and build**

Run: `cd frontend && npm run build`
Expected: exit 0, no TypeScript errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src
git commit -m "feat: Start/End here trim controls and Re-render on clip cards"
```

---

### Task 6: End-to-end check

- [ ] **Step 1:** Start the backend (`.venv/Scripts/python backend/run.py`) and open the built app; open a finished job (or run one in API mode with a short video).
- [ ] **Step 2:** On one clip, pause partway in, click Start here, seek near the end, click End here; confirm the bar shades the cut and two marks are listed.
- [ ] **Step 3:** Click Re-render. Expected in the job log: `Re-rendering clip N: …`, `reusing raw download` for that clip, `already rendered` for every other clip, and no `downloading` line.
- [ ] **Step 4:** Confirm the card's video reloads, is shorter, and starts/ends on the marked words; Approve is cleared, notes kept, one applied trim listed.
- [ ] **Step 5:** Run the full test suite: `.venv/Scripts/python -m pytest tests backend/tests -q` → all PASS.
