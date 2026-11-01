"""Review marks: a reviewer marks Start/End on a rendered clip, then re-renders it trimmed to them.

Marks can only shrink a clip, so the re-render reuses the clip's original raw download.
"""

from __future__ import annotations

from app.store import now
from clipper.candidates import snap_mark, words_text
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
            return c
    raise TrimError("Clip not found", 404)


def add_mark(job: dict, clip_id: int, kind: str, t: float, segments: list[dict]) -> dict:
    clip = _clip(job, clip_id)
    if not 0 <= t <= clip["duration_sec"]:
        raise TrimError(f"Time {t:.2f}s is outside the clip (0-{clip['duration_sec']:.2f}s)")
    t0 = clip.get("source_t0")
    t0 = clip["start_sec"] - HEAD_ROOM if t0 is None else t0
    # Clamp to the pick, not the rendered clip: after a failed re-render the pick has already moved.
    pick = next((p for p in job.get("picks") or [] if p["id"] == clip_id), clip)
    lo, hi = max(clip["start_sec"], pick["start_sec"]), min(clip["end_sec"], pick["end_sec"])
    mark = {"kind": kind, "t": round(t, 3),
            "source_sec": snap_mark(t0 + t, kind, segments, lo, hi), "at": now()}
    clip["review"]["marks"].append(mark)
    return mark


def clear_marks(job: dict, clip_id: int) -> None:
    _clip(job, clip_id)["review"]["marks"] = []


def apply_trim(job: dict, clip_id: int, segments: list[dict]) -> tuple[tuple[float, float], tuple[float, float]]:
    """Move the clip's pick to its pending marks; the caller re-queues the job."""
    if job["status"] in BUSY:
        raise TrimError(f"Job is {job['status']}", 409)
    clip = _clip(job, clip_id)
    marks = clip["review"]["marks"]
    if not marks:
        raise TrimError("Mark a new start or end first")
    pick = next((p for p in job["picks"] or [] if p["id"] == clip_id), None)
    if pick is None:
        raise TrimError("Clip not found", 404)
    latest = {m["kind"]: m["source_sec"] for m in marks}
    old = (pick["start_sec"], pick["end_sec"])
    new = (latest.get("start", old[0]), latest.get("end", old[1]))
    if new[1] - new[0] < MIN_TRIM_SEC:
        raise TrimError(f"The trimmed clip must be at least {MIN_TRIM_SEC:g} s long and start before it ends")
    pick.update(start_sec=new[0], end_sec=new[1], text=words_text(segments, *new))
    review = clip["review"]
    review["trims"].append({"from": list(old), "to": list(new), "at": now()})
    review["marks"] = []
    review["approved"] = None
    clip["rerendering"] = True
    return old, new
