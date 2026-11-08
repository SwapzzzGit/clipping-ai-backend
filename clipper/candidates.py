"""Step 2: find viral clip candidates with a cheap LLM.

Pass 1 scores overlapping transcript windows in batches; pass 2 asks the model to pick
and trim the final clips from the best regions. Everything the model returns is
validated locally: times are clamped, snapped to sentence boundaries, length-limited
and de-overlapped, so a sloppy reply can never produce a broken clip.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from clipper.llm import LLM
from clipper.music import DEFAULT_MOOD, clean_mood
from clipper.util import ClipperError, extract_json, log, read_json, write_json

CRITERIA = ["hook_strength", "emotion", "suspense_or_curiosity", "controversy_or_surprise", "standalone_clarity"]
SENTENCE_END = re.compile(r"[.?!][\"')\]]*$")
PAUSE_SEC = 0.6

PASS1_SYSTEM = """You are a senior short-form video editor who knows what goes viral on TikTok, Reels and Shorts.
You will get numbered transcript windows from a long podcast/video. Score EACH window from 0 to 10 on:
- hook_strength: would the opening seconds stop someone scrolling?
- emotion: strong feeling (funny, angry, moving, inspiring, shocking)
- suspense_or_curiosity: creates a question the viewer needs answered
- controversy_or_surprise: hot take, counter-intuitive claim or surprising fact
- standalone_clarity: fully understandable with ZERO prior context (no "as I said", unexplained names, etc.)
Be harsh: most windows are 2-5; reserve 8+ for genuinely exceptional moments.
Reply with ONLY this JSON, one entry per window id:
{"scores": [{"id": 0, "hook_strength": 0, "emotion": 0, "suspense_or_curiosity": 0, "controversy_or_surprise": 0, "standalone_clarity": 0, "violates_rules": false, "reason": "one short line"}]}"""

PASS2_RULES = """Rules for every clip:
- start_sec is the start time of a line that opens with a strong hook sentence (no "and", "so", "but yeah" openers)
- end_sec is right after a complete thought ends (the end of a sentence, never mid-sentence)
- {min_s}-{max_s} seconds long
- clips must not overlap each other; at most one clip per region unless the region is long
- title: punchy, <= 60 characters, no clickbait lies; hook_line: the clip's exact first sentence
- music_mood: the background-music mood that suits the clip: calm, upbeat, tense, emotional, serious or funny
Reply with ONLY this JSON:
{{"clips": [{{"region_id": 0, "start_sec": 0.0, "end_sec": 0.0, "title": "", "hook_line": "", "reason": "why this will perform", "music_mood": "calm"}}]}}"""

PASS2_SYSTEM = """You are a senior short-form video editor. From the candidate regions of a long video, choose the
{n} best short-form clips (TikTok/Reels/Shorts). Each transcript line is prefixed with its start time in seconds.
""" + PASS2_RULES

PASS2_AUTO_SYSTEM = """You are a senior short-form video editor. From the candidate regions of a long video, choose up to
{n} short-form clips (TikTok/Reels/Shorts) that would plausibly go viral on their own. Pick only strong moments: fewer
clips beat weak ones, but always return at least one. Each transcript line is prefixed with its start time in seconds.
""" + PASS2_RULES


def split_rules(rules: str) -> tuple[str, str]:
    """(hard, preferred): lines that start with "DO" are preferences to weigh; every other line is a hard limit."""
    hard, preferred = [], []
    for line in (rules or "").splitlines():
        (preferred if line.strip().upper().startswith("DO ") else hard).append(line)
    return "\n".join(hard).strip(), "\n".join(preferred).strip()


def rules_block(rules: str) -> str:
    hard, preferred = split_rules(rules)
    block = ""
    if hard:
        block += ("\n\nHARD CONSTRAINTS (campaign rules). These override everything else. Never select or "
                  "favour content that breaks a DON'T rule; if a window breaks one, set every score to 0 and "
                  f'"violates_rules": true.\n{hard}')
    if preferred:
        block += f"\n\nPREFERENCES (weigh these between good moments; missing one is not a violation):\n{preferred}"
    return block


# -- windows -------------------------------------------------------------------------


def build_windows(segments: list[dict], length: float, stride: float, min_len: float = 20) -> list[dict]:
    """Overlapping windows aligned to caption segment starts."""
    if not segments:
        return []
    starts = [s["start_sec"] for s in segments]
    last_end = segments[-1]["end_sec"]
    windows, seen, t = [], set(), starts[0]
    while t < last_end:
        i = bisect_left(starts, t)
        if i >= len(segments):
            break
        if i not in seen:
            seen.add(i)
            j = i
            while j + 1 < len(segments) and segments[j + 1]["start_sec"] < segments[i]["start_sec"] + length:
                j += 1
            start, end = segments[i]["start_sec"], segments[j]["end_sec"]
            contained = windows and end <= windows[-1]["end_sec"]  # tail window adds nothing new
            if not contained and (end - start >= min_len or not windows):
                windows.append({
                    "id": len(windows),
                    "start_sec": start,
                    "end_sec": end,
                    "text": " ".join(s["text"] for s in segments[i : j + 1]),
                })
        t += stride
    return windows


def heat_for(start: float, end: float, heatmap: list[dict]) -> float:
    vals = [h["value"] for h in heatmap if h["start_sec"] < end and h["end_sec"] > start]
    return max(vals) if vals else 0.0


def final_score(scores: dict, weights: dict, heat: float, bonus: float, threshold: float) -> float:
    total_w = sum(weights.get(c, 0) for c in CRITERIA) or 1
    base = sum(scores[c] * weights.get(c, 0) for c in CRITERIA) / total_w
    boost = bonus * heat if heat >= threshold else 0.0
    return round(base + boost, 3)


# -- pass 1 --------------------------------------------------------------------------


def validate_pass1(reply: object, ids: list[int]) -> dict[int, dict]:
    items = reply.get("scores") if isinstance(reply, dict) else reply
    if not isinstance(items, list):
        raise ValueError('expected {"scores": [...]}')
    out = {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") not in ids:
            continue
        entry = {}
        for c in CRITERIA:
            v = item.get(c)
            try:
                if isinstance(v, bool):
                    raise TypeError
                v = float(v)  # also accepts numeric strings like "7"
            except (TypeError, ValueError):
                raise ValueError(f"window {item['id']}: {c} must be a number, got {v!r}")
            entry[c] = max(0.0, min(10.0, v))
        entry["violates_rules"] = bool(item.get("violates_rules", False))
        entry["reason"] = str(item.get("reason", ""))[:300]
        out[item["id"]] = entry
    missing = [i for i in ids if i not in out]
    if missing:
        raise ValueError(f"missing scores for window ids {missing}")
    return out


def ask_json(llm: LLM, system: str, user: str, validate, fallback=None):
    """Call the LLM, validate its JSON, retry once with the error message on failure."""
    error = None
    for attempt in range(2):
        prompt = user if error is None else (
            f"{user}\n\nYour previous reply was invalid ({error}). Reply with valid JSON only, exactly in the format given."
        )
        reply = llm.complete(system, prompt, fallback=fallback)
        try:
            return validate(extract_json(reply))
        except (ValueError, KeyError, TypeError) as e:
            error = str(e)[:200]
            log.warning("LLM reply invalid (attempt %d): %s", attempt + 1, error)
    raise ValueError(error)


def score_windows(windows: list[dict], llm: LLM, cfg: dict, rules: str, cache_path: Path) -> dict[int, dict]:
    """Pass 1, cached per window so an interrupted run resumes where it stopped."""
    meta = {
        "model": llm.name,
        "windows": cfg["windows"],
        "rules": hashlib.sha1(rules.encode()).hexdigest()[:10],
        "n_windows": len(windows),
    }
    cached = read_json(cache_path) if cache_path.is_file() else {}
    scores: dict[int, dict] = (
        {int(k): v for k, v in cached.get("scores", {}).items()} if cached.get("meta") == meta else {}
    )
    todo = [w for w in windows if w["id"] not in scores]
    if not todo:
        log.info("pass 1: all %d windows cached", len(windows))
        return scores

    size = cfg["windows"]["batch_size"]
    batches = [todo[i : i + size] for i in range(0, len(todo), size)]
    system = PASS1_SYSTEM + rules_block(rules)
    log.info("pass 1: scoring %d windows in %d LLM calls (%s)", len(todo), len(batches), llm.name)

    def run(batch):
        user = "\n\n".join(f"### Window {w['id']}\n{w['text']}" for w in batch)
        ids = [w["id"] for w in batch]
        try:
            return ask_json(llm, system, user, lambda r: validate_pass1(r, ids), fallback=lambda: mock_pass1(batch))
        except (ValueError, ClipperError) as e:
            # Skip this batch; it stays uncached, so the next run retries just these windows.
            log.warning("skipping windows %s-%s: %s", ids[0], ids[-1], str(e)[:200])
            return None

    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, cfg["llm"]["concurrency"])) as pool:
        futures = [pool.submit(run, b) for b in batches]
        for done, fut in enumerate(as_completed(futures), 1):
            result = fut.result()
            if result is None:
                failed += 1
            else:
                scores.update(result)
                write_json(cache_path, {"meta": meta, "scores": scores})
            if done % 10 == 0 or done == len(batches):
                log.info("pass 1: %d/%d batches (%d failed)", done, len(batches), failed)
    if failed > len(batches) / 2 or not scores:
        raise ClipperError(
            f"{failed}/{len(batches)} scoring batches failed (see warnings). "
            "Progress is cached: re-run the same command to retry only the missing windows."
        )
    if failed:
        log.warning("%d batches unscored; continuing with %d/%d windows", failed, len(scores), len(windows))
    return scores


# -- sentence boundaries -------------------------------------------------------------


def boundaries(segments: list[dict]) -> dict[str, list[float]]:
    """Candidate clip start/end times, split into strong (punctuation) and weak (pause) ones."""
    words = _words(segments)
    if not words:  # no speech: no sentence boundaries, so clip edges are left where they are
        return {"strong_start": [], "strong_end": [], "weak_start": [], "weak_end": [], "word_start": [], "word_end": []}
    strong_s, strong_e, weak_s, weak_e = [words[0]["start"]], [], [words[0]["start"]], []
    for prev, cur in zip(words, words[1:]):
        if SENTENCE_END.search(prev["text"]) or cur.get("speaker_change"):
            strong_e.append(prev["end"])
            strong_s.append(cur["start"])
        elif cur["start"] - prev["end"] >= PAUSE_SEC:
            weak_e.append(prev["end"])
            weak_s.append(cur["start"])
    strong_e.append(words[-1]["end"])
    weak_e.append(words[-1]["end"])
    return {
        "strong_start": strong_s, "strong_end": strong_e, "weak_start": weak_s, "weak_end": weak_e,
        "word_start": [w["start"] for w in words], "word_end": [w["end"] for w in words],
    }


def _words(segments: list[dict]) -> list[dict]:
    """Every word of the transcript in time order. A line without word timings (YouTube's auto-captions
    often have some) has its words spread evenly over its time, so its sentence ends and text still count."""
    words = []
    for s in segments:
        if s["words"]:
            words += s["words"]
            continue
        tokens = s["text"].split()
        span = (s["end_sec"] - s["start_sec"]) / max(1, len(tokens))
        words += [{"start": s["start_sec"] + i * span, "end": s["start_sec"] + (i + 1) * span, "text": tok}
                  for i, tok in enumerate(tokens)]
    return words


MARK_TOLERANCE = 0.15


def snap_mark(source: float, kind: str, segments: list[dict], lo: float, hi: float) -> float:
    """Snap a reviewer's Start/End mark to a word edge, clamped to the clip's current [lo, hi].

    The word under the playhead is kept unless only its last (start) or first (end) 0.15 s is left.
    """
    words = _words(segments)
    if kind == "start":
        t = next((w["start"] for w in words if w["end"] > source + MARK_TOLERANCE), hi)
    else:
        t = next((w["end"] for w in reversed(words) if w["start"] < source - MARK_TOLERANCE), lo)
    return min(max(t, lo), hi)


def words_text(segments: list[dict], start: float, end: float) -> str:
    return " ".join(w["text"] for w in _words(segments) if w["start"] >= start - 0.01 and w["end"] <= end + 0.01)


def words_between(segments: list[dict], start: float, end: float) -> list[dict]:
    """The transcript's words that fall inside a clip's range, in time order."""
    return [w for w in _words(segments) if w["start"] >= start - 0.01 and w["end"] <= end + 0.01]


def _nearest(points: list[float], t: float, tol: float, lo: float = float("-inf"), hi: float = float("inf")):
    best = None
    for p in points:
        if lo <= p <= hi and abs(p - t) <= tol and (best is None or abs(p - t) < abs(best - t)):
            best = p
    return best


def snap(t: float, b: dict, kind: str, tol: float, lo=float("-inf"), hi=float("inf")) -> float:
    for key in (f"strong_{kind}", f"weak_{kind}"):
        p = _nearest(b[key], t, tol, lo, hi)
        if p is not None:
            return p
    p = _nearest(b[f"word_{kind}"], t, tol, lo, hi)
    return p if p is not None else t


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9']", "", text.lower())


def align_hook(hook: str, approx: float, segments: list[dict], before: float = 15, after: float = 45) -> float | None:
    """Start time of the word where `hook` begins, searched near `approx`.

    The LLM only sees line-level timestamps, so its start is often a few words early
    (e.g. "pretty quickly, huh? So the only way out..."); the hook text pins the real start.
    """
    want = [t for t in (_norm(x) for x in hook.split()) if t][:5]
    if len(want) < 2:
        return None
    words = [w for w in _words(segments) if approx - before <= w["start"] <= approx + after]
    toks = [_norm(w["text"]) for w in words]
    for i in range(len(words) - len(want) + 1):
        if toks[i : i + len(want)] == want:
            return words[i]["start"]
    return None


def fit_clip(start: float, end: float, b: dict, cfg: dict, snap_start: bool = True) -> tuple[float, float] | None:
    """Snap the start to a sentence start, and end on a sentence end inside [min_sec, max_sec].

    A clip never ends mid-sentence: if no sentence end fits the length limits, the pick is dropped.
    """
    mn, mx, tol = cfg["clips"]["min_sec"], cfg["clips"]["max_sec"], cfg["clips"]["snap_tolerance_sec"]
    if snap_start:
        start = snap(start, b, "start", tol)
    ends = [p for p in b["strong_end"] if start + mn <= p <= start + mx]
    if not ends:
        return None
    end = min(ends, key=lambda p: abs(p - end))
    return round(start, 3), round(end, 3)


# -- pass 2 --------------------------------------------------------------------------


def merge_regions(windows: list[dict]) -> list[dict]:
    regions = []
    for w in sorted(windows, key=lambda w: w["start_sec"]):
        if regions and w["start_sec"] <= regions[-1]["end_sec"]:
            r = regions[-1]
            r["end_sec"] = max(r["end_sec"], w["end_sec"])
            r["windows"].append(w)
        else:
            regions.append({"start_sec": w["start_sec"], "end_sec": w["end_sec"], "windows": [w]})
    for i, r in enumerate(regions):
        r["id"] = i
        r["score"] = max(w["final_score"] for w in r["windows"])
    return regions


def region_text(region: dict, segments: list[dict]) -> str:
    lines = [
        f"[{s['start_sec']:.1f}] {s['text']}"
        for s in segments
        if s["start_sec"] >= region["start_sec"] - 0.01 and s["start_sec"] < region["end_sec"]
    ]
    return "\n".join(lines)


def clip_text(segments: list[dict], start: float, end: float) -> str:
    """Exact words spoken inside [start, end] (segment-level when there are no word timings)."""
    eps = 0.05
    words = [w["text"] for s in segments for w in s["words"] if w["start"] >= start - eps and w["start"] < end - eps]
    if words:
        return " ".join(words)
    return " ".join(s["text"] for s in segments if s["end_sec"] > start + eps and s["start_sec"] < end - eps)


def overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def clip_score(start: float, end: float, windows: list[dict]) -> dict:
    """Score a clip by the best window that covers a meaningful part of it."""
    def cover(w):
        return min(end, w["end_sec"]) - max(start, w["start_sec"])

    relevant = [w for w in windows if cover(w) >= min(10, (end - start) / 2)]
    if not relevant:
        relevant = sorted(windows, key=cover, reverse=True)[:1]
    best = max(relevant, key=lambda w: w["final_score"])
    return {
        "score": best["final_score"],
        "score_breakdown": {c: best["scores"][c] for c in CRITERIA},
        "heatmap": best["heat"],
        "window_reason": best["scores"]["reason"],
    }


def keep_strong(clips: list[dict], cfg: dict) -> list[dict]:
    """Auto count: keep the clips that score well, and always keep the best one."""
    if not cfg.get("auto_clips"):
        return clips
    ranked = sorted(clips, key=lambda c: c["score"], reverse=True)
    return [c for c in ranked if c["score"] >= cfg["auto_min_score"]] or ranked[:1]


def select_clips(regions, segments, scored_windows, llm: LLM, cfg: dict, rules: str) -> list[dict]:
    n = cfg["num_clips"]
    b = boundaries(segments)
    template = PASS2_AUTO_SYSTEM if cfg.get("auto_clips") else PASS2_SYSTEM
    system = template.format(n=n, min_s=cfg["clips"]["min_sec"], max_s=cfg["clips"]["max_sec"]) + rules_block(rules)
    user = "\n\n".join(
        f"### Region {r['id']} ({r['start_sec']:.1f}s-{r['end_sec']:.1f}s, pass-1 score {r['score']:.1f})\n"
        + region_text(r, segments)
        for r in regions
    )
    by_id = {r["id"]: r for r in regions}

    def validate(reply):
        items = reply.get("clips") if isinstance(reply, dict) else reply
        if not isinstance(items, list) or not items:
            raise ValueError('expected {"clips": [...]}')
        for c in items:
            if c.get("region_id") not in by_id:
                raise ValueError(f"unknown region_id {c.get('region_id')}")
            float(c["start_sec"]), float(c["end_sec"])
        return items

    try:
        picks = ask_json(llm, system, user, validate, fallback=lambda: mock_pass2(regions, n))
    except ValueError as e:
        log.warning("pass 2 failed (%s); falling back to top-scored regions", e)
        picks = []

    chosen: list[dict] = []
    used_regions = set()
    for p in picks:
        if len(chosen) >= n:
            break
        r = by_id[p["region_id"]]
        start = min(max(float(p["start_sec"]), r["start_sec"]), r["end_sec"])
        end = min(max(float(p["end_sec"]), start), r["end_sec"] + cfg["clips"]["snap_tolerance_sec"])
        hook = str(p.get("hook_line", "")).strip()
        hook_at = align_hook(hook, start, segments, after=max(45, end - start))
        if hook_at is not None:
            end += hook_at - start if end - hook_at < cfg["clips"]["min_sec"] else 0
            start = hook_at
        else:
            hook = ""  # the model's hook isn't where the clip starts; derive it from the transcript below
        fitted = fit_clip(start, end, b, cfg, snap_start=hook_at is None)
        if not fitted or any(overlaps(fitted, (c["start_sec"], c["end_sec"])) for c in chosen):
            log.info("dropping LLM pick %s-%s (invalid or overlapping)", p["start_sec"], p["end_sec"])
            continue
        chosen.append({
            "start_sec": fitted[0], "end_sec": fitted[1],
            "title": str(p.get("title", "")).strip()[:80],
            "hook_line": hook,
            "reason": str(p.get("reason", "")).strip(),
            "music_mood": clean_mood(p.get("music_mood")),
            "selected_by": "llm",
        })
        used_regions.add(r["id"])

    # Top up from the best remaining regions if the model returned too few valid clips.
    for r in sorted(regions, key=lambda r: r["score"], reverse=True):
        if len(chosen) >= n:
            break
        if r["id"] in used_regions:
            continue
        w = max(r["windows"], key=lambda w: w["final_score"])
        fitted = fit_clip(w["start_sec"], w["start_sec"] + min(45, cfg["clips"]["max_sec"]), b, cfg)
        if not fitted or any(overlaps(fitted, (c["start_sec"], c["end_sec"])) for c in chosen):
            continue
        chosen.append({
            "start_sec": fitted[0], "end_sec": fitted[1], "title": "", "hook_line": "",
            "reason": w["scores"]["reason"], "music_mood": DEFAULT_MOOD, "selected_by": "fallback",
        })

    for c in chosen:
        c.update(clip_score(c["start_sec"], c["end_sec"], scored_windows))
        c["text"] = clip_text(segments, c["start_sec"], c["end_sec"])
        if not c["hook_line"]:
            c["hook_line"] = re.split(r"(?<=[.?!])\s", c["text"], maxsplit=1)[0][:160]
        if not c["title"]:
            c["title"] = c["hook_line"][:60]
    chosen = keep_strong(chosen, cfg)
    chosen.sort(key=lambda c: c["score"], reverse=True)
    return [{"id": i + 1, **c} for i, c in enumerate(chosen)]


# -- orchestration -------------------------------------------------------------------


def find_clips(transcript: dict, work: Path, llm: LLM, cfg: dict, rules: str, force: bool = False) -> list[dict]:
    cache = work / "clips.json"
    meta = {"model": llm.name, "num_clips": cfg["num_clips"], "auto_clips": cfg.get("auto_clips", False),
            "clips": cfg["clips"], "windows": cfg["windows"],
            "scoring": cfg["scoring"], "rules": hashlib.sha1(rules.encode()).hexdigest()[:10]}
    if cache.is_file() and not force:
        cached = read_json(cache)
        if cached.get("meta") == meta and not cached.get("unscored_windows"):
            log.info("clips.json cached (%d clips)", len(cached["clips"]))
            return cached["clips"]
        if cached.get("meta") == meta:
            log.info("previous run left %d windows unscored; retrying them", cached["unscored_windows"])

    segments = transcript["segments"]
    windows = build_windows(segments, cfg["windows"]["length_sec"], cfg["windows"]["stride_sec"], cfg["clips"]["min_sec"])
    write_json(work / "windows.json", windows)
    scores = score_windows(windows, llm, cfg, rules, work / "scores.json")

    sc = cfg["scoring"]
    scored = []
    for w in windows:
        s = scores.get(w["id"])
        if not s or s["violates_rules"]:
            continue
        heat = heat_for(w["start_sec"], w["end_sec"], transcript["heatmap"])
        scored.append({**w, "scores": s, "heat": heat,
                       "final_score": final_score(s, sc["weights"], heat, sc["heatmap_bonus"], sc["heatmap_threshold"])})
    if not scored:
        raise ClipperError("No usable windows after scoring (all failed or broke campaign rules).")
    write_json(work / "windows_scored.json", sorted(scored, key=lambda w: w["final_score"], reverse=True))

    top = sorted(scored, key=lambda w: w["final_score"], reverse=True)[: sc["top_k"]]
    regions = merge_regions(top)
    log.info("pass 2: choosing %d clips from %d regions", cfg["num_clips"], len(regions))
    clips = select_clips(regions, segments, scored, llm, cfg, rules)
    if not clips:
        raise ClipperError("Could not build any valid clip from the candidates.")
    # A partial result is still used now, but the next run retries the missing windows first.
    write_json(cache, {"meta": meta, "unscored_windows": len(windows) - len(scores), "clips": clips})
    return clips


def normalize_external_picks(picks: list[dict], transcript: dict, cfg: dict) -> list[dict]:
    """Validate clip picks made outside the LLM path (e.g. by Claude via the web app).

    Same guarantees as pass 2: start aligned to the hook sentence, end on a sentence
    boundary, length within [min_sec, max_sec], no overlaps. Each pick is
    {start_sec, end_sec, title?, hook_line?, reason?, score?, score_breakdown?}.
    """
    segments, heatmap = transcript["segments"], transcript["heatmap"]
    b = boundaries(segments)
    chosen: list[dict] = []
    for p in picks:
        if len(chosen) >= cfg["num_clips"]:
            break
        try:
            start, end = float(p["start_sec"]), float(p["end_sec"])
        except (KeyError, TypeError, ValueError):
            log.warning("ignoring pick without numeric start_sec/end_sec: %s", str(p)[:120])
            continue
        hook = str(p.get("hook_line") or "").strip()
        hook_at = align_hook(hook, start, segments, after=max(45, end - start)) if hook else None
        if hook_at is not None:
            end += hook_at - start if end - hook_at < cfg["clips"]["min_sec"] else 0
            start = hook_at
        else:
            hook = ""
        fitted = fit_clip(start, end, b, cfg, snap_start=hook_at is None)
        if not fitted or any(overlaps(fitted, (c["start_sec"], c["end_sec"])) for c in chosen):
            log.info("dropping pick %s-%s (invalid or overlapping)", p.get("start_sec"), p.get("end_sec"))
            continue
        breakdown = p.get("score_breakdown") if isinstance(p.get("score_breakdown"), dict) else {}
        try:
            score = max(0.0, min(10.0, float(p.get("score", 0))))
        except (TypeError, ValueError):
            score = 0.0
        text = clip_text(segments, *fitted)
        hook = hook or re.split(r"(?<=[.?!])\s", text, maxsplit=1)[0][:160]
        chosen.append({
            "start_sec": fitted[0], "end_sec": fitted[1],
            "title": (str(p.get("title") or "").strip() or hook)[:80],
            "hook_line": hook, "reason": str(p.get("reason") or "").strip(),
            "selected_by": p.get("selected_by", "claude"),
            "music_mood": clean_mood(p.get("music_mood")),
            "score": round(score, 2),
            "score_breakdown": {k: v for k, v in breakdown.items() if isinstance(v, (int, float))},
            "heatmap": heat_for(fitted[0], fitted[1], heatmap),
            "text": text,
        })
    chosen = keep_strong(chosen, cfg)
    chosen.sort(key=lambda c: c["score"], reverse=True)
    return [{"id": i + 1, **c} for i, c in enumerate(chosen)]


# -- mock provider responses (LLM_PROVIDER=mock) -------------------------------------

_HOOKY = re.compile(r"\b(never|secret|crazy|truth|actually|mistake|why|how|nobody|everyone|best|worst|money|died?|love|hate)\b", re.I)


def mock_pass1(batch: list[dict]) -> str:
    out = []
    for w in batch:
        t = w["text"]
        hook = min(10, 2 + 1.5 * t[:200].count("?") + len(_HOOKY.findall(t[:200])))
        emo = min(10, 2 + 2 * t.count("!") + len(_HOOKY.findall(t)) / 3)
        cur = min(10, 2 + t.count("?"))
        sur = min(10, 2 + len(re.findall(r"\d", t)) / 4)
        out.append({"id": w["id"], "hook_strength": hook, "emotion": emo, "suspense_or_curiosity": cur,
                    "controversy_or_surprise": sur, "standalone_clarity": 5, "violates_rules": False,
                    "reason": "mock heuristic score"})
    return json.dumps({"scores": out})


def mock_pass2(regions: list[dict], n: int) -> str:
    best = sorted(regions, key=lambda r: r["score"], reverse=True)[:n]
    return json.dumps({"clips": [
        {"region_id": r["id"], "start_sec": r["start_sec"], "end_sec": r["start_sec"] + 40,
         "title": "", "hook_line": "", "reason": "mock pick"} for r in best
    ]})
