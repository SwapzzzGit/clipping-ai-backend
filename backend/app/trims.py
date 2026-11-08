"""Review edits on a rendered clip.

Marks move the clip's Start and End, and the re-render reuses the clip's original raw download, so marks can only
shrink a clip. Cuts remove the words a reviewer deletes: they are kept on the pick in source seconds, and the
render cuts those words out of the finished clip, so the picture, the captions and the sound close the gap together.
"""

from __future__ import annotations

import re

from app.store import now
from clipper.candidates import snap_mark, words_between, words_text
from clipper.render import HEAD_ROOM

BUSY = {"queued", "running", "cancelling", "awaiting_claude"}
MIN_TRIM_SEC = 3.0


class TrimError(Exception):
    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


def _clip(job: dict, clip_id: int) -> dict:
    for c in job["clips"]:
        if c["id"] == clip_id:
            review = c.setdefault("review", {"approved": None, "notes": ""})
            review.setdefault("marks", [])
            review.setdefault("trims", [])
            review.setdefault("cuts", [])
            return c
    raise TrimError("Clip not found", 404)


def _pick(job: dict, clip_id: int) -> dict:
    pick = next((p for p in job["picks"] or [] if p["id"] == clip_id), None)
    if pick is None:
        raise TrimError("Clip not found", 404)
    return pick


def _cut_key(c: dict) -> tuple[float, float]:
    return (round(c["s"], 2), round(c["e"], 2))


def cuts_pending(clip: dict) -> bool:
    """True when the reviewer's cuts differ from the cuts already in the rendered file."""
    wanted = {_cut_key(c) for c in clip["review"].get("cuts", [])}
    baked = {_cut_key(c) for c in clip.get("cuts_baked") or []}
    return wanted != baked


def add_mark(job: dict, clip_id: int, kind: str, t: float, segments: list[dict], snap: bool = True) -> dict:
    clip = _clip(job, clip_id)
    if not 0 <= t <= clip["duration_sec"]:
        raise TrimError(f"Time {t:.2f}s is outside the clip (0-{clip['duration_sec']:.2f}s)")
    t0 = clip.get("source_t0")
    t0 = clip["start_sec"] - HEAD_ROOM if t0 is None else t0
    # Clamp to the pick, not the rendered clip: after a failed re-render the pick has already moved.
    pick = next((p for p in job.get("picks") or [] if p["id"] == clip_id), clip)
    lo, hi = max(clip["start_sec"], pick["start_sec"]), min(clip["end_sec"], pick["end_sec"])
    src = t0 + t
    # snap=False keeps the exact frame the reviewer chose, still clamped to the pick.
    source = snap_mark(src, kind, segments, lo, hi) if snap else min(max(src, lo), hi)
    mark = {"kind": kind, "t": round(t, 3), "source_sec": round(source, 3), "exact": not snap, "at": now()}
    clip["review"]["marks"].append(mark)
    return mark


def clear_marks(job: dict, clip_id: int) -> None:
    _clip(job, clip_id)["review"]["marks"] = []


def add_cut(job: dict, clip_id: int, s: float, e: float, text: str) -> dict:
    """Mark the words from s to e (source seconds) for removal from the clip."""
    if job["status"] in BUSY:
        raise TrimError(f"Job is {job['status']}", 409)
    clip = _clip(job, clip_id)
    pick = _pick(job, clip_id)
    cuts = clip["review"]["cuts"]
    if not (pick["start_sec"] - 0.01 <= s < e <= pick["end_sec"] + 0.01):
        raise TrimError("Those words are outside this clip")
    if any(s < c["e"] - 0.005 and c["s"] < e - 0.005 for c in cuts):
        raise TrimError("Those words are already cut", 409)
    left = (pick["end_sec"] - pick["start_sec"]) - sum(c["e"] - c["s"] for c in cuts) - (e - s)
    if left < MIN_TRIM_SEC:
        raise TrimError(f"The clip must keep at least {MIN_TRIM_SEC:g} s after the cut")
    cut = {"s": round(s, 3), "e": round(e, 3), "text": text.strip()[:200], "at": now()}
    cuts.append(cut)
    cuts.sort(key=lambda c: c["s"])
    pick["cuts"] = [dict(c) for c in cuts]
    return cut


def restore_cut(job: dict, clip_id: int, s: float, e: float) -> None:
    """Put back the words of a cut. They play again after the next re-render."""
    if job["status"] in BUSY:
        raise TrimError(f"Job is {job['status']}", 409)
    clip = _clip(job, clip_id)
    pick = _pick(job, clip_id)
    cuts = clip["review"]["cuts"]
    hit = next((c for c in cuts if abs(c["s"] - s) < 0.01 and abs(c["e"] - e) < 0.01), None)
    if hit is None:
        raise TrimError("That cut is not found", 404)
    cuts.remove(hit)
    pick["cuts"] = [dict(c) for c in cuts]


def apply_trim(job: dict, clip_id: int, segments: list[dict]) -> tuple[tuple[float, float], tuple[float, float]]:
    """Move the clip's pick to its pending marks and cuts; the caller re-queues the job."""
    if job["status"] in BUSY:
        raise TrimError(f"Job is {job['status']}", 409)
    clip = _clip(job, clip_id)
    marks = clip["review"]["marks"]
    pick = _pick(job, clip_id)
    old = (pick["start_sec"], pick["end_sec"])
    if marks:
        latest = {m["kind"]: m["source_sec"] for m in marks}
        new = (latest.get("start", old[0]), latest.get("end", old[1]))
        if new[1] - new[0] < MIN_TRIM_SEC:
            raise TrimError(f"The trimmed clip must be at least {MIN_TRIM_SEC:g} s long and start before it ends")
        pick.update(start_sec=new[0], end_sec=new[1], text=words_text(segments, *new))
    elif cuts_pending(clip):
        new = old
    else:
        raise TrimError("Mark a new start or end, or cut some words, first")
    # Cuts outside the clip's new range no longer apply.
    cuts = [c for c in clip["review"]["cuts"] if c["s"] >= new[0] - 0.01 and c["e"] <= new[1] + 0.01]
    clip["review"]["cuts"] = cuts
    pick["cuts"] = [dict(c) for c in cuts]
    review = clip["review"]
    if marks:
        review["trims"].append({"from": list(old), "to": list(new), "at": now()})
    review["marks"] = []
    review["approved"] = None
    clip["rerendering"] = True
    return old, new


FILLER_MAX_WORDS = 20


def _plain(text: str) -> str:
    """A word the way the filler list compares it: lower case, letters and apostrophes only."""
    return re.sub(r"[^a-z']", "", text.lower())


def set_fillers(job: dict, clip_id: int, segments: list[dict], on: bool, words: list[str]) -> None:
    """Cut the filler words (for example "so") out of the clip, or put them back.

    The cuts this adds are marked "filler", so turning the setting off removes only those. Cuts the reviewer
    made by hand stay, and a filler that would overlap one of them, or leave less than MIN_TRIM_SEC, is skipped.
    """
    if job["status"] in BUSY:
        raise TrimError(f"Job is {job['status']}", 409)
    words = [w.strip() for w in words if w.strip()]
    if len(words) > FILLER_MAX_WORDS or any(len(w) > 30 for w in words):
        raise TrimError(f"Use up to {FILLER_MAX_WORDS} filler words of 30 letters or fewer")
    clip = _clip(job, clip_id)
    pick = _pick(job, clip_id)
    review = clip["review"]
    cuts = [c for c in review["cuts"] if c.get("auto") != "filler"]
    if on:
        wanted = {_plain(w) for w in words} - {""}
        length = pick["end_sec"] - pick["start_sec"]
        for w in words_between(segments, pick["start_sec"], pick["end_sec"]):
            if _plain(w["text"]) not in wanted:
                continue
            s, e = round(w["start"], 3), round(w["end"], 3)
            if any(s < c["e"] - 0.005 and c["s"] < e - 0.005 for c in cuts):
                continue
            if length - sum(c["e"] - c["s"] for c in cuts) - (e - s) < MIN_TRIM_SEC:
                continue
            cuts.append({"s": s, "e": e, "text": w["text"].strip(), "at": now(), "auto": "filler"})
    cuts.sort(key=lambda c: c["s"])
    review["cuts"] = cuts
    review["fillers"] = {"on": on, "words": words}
    pick["cuts"] = [dict(c) for c in cuts]
