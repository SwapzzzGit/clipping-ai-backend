"""Clip picks for video without speech (gaming footage, music, silent screen recordings).

With no words to read, the moments are chosen from what the picture and the sound do:
how much the picture changes between samples, how often it changes a lot, and how loud
the audio gets. This is a heuristic. It finds the busiest moments, not the funniest ones.
"""

from __future__ import annotations

import subprocess

import cv2
import numpy as np

from clipper.util import log

SAMPLE_FPS = 2.0      # picture samples per second
AUDIO_RATE = 4000     # Hz of the mono audio used for loudness (speech-level detail is not needed)
WIDTH = 160           # picture width used for motion; small keeps decoding fast
ACTIVE = 0.5          # motion at or above this share of the busiest 5% counts as "in action"
MIN_GAP_SEC = 3.0     # gap kept between two picked moments
STEP_SEC = 2.0        # how far the candidate window moves each time


def _dimensions(src) -> tuple[int, int]:
    cap = cv2.VideoCapture(str(src))
    try:
        w, h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    finally:
        cap.release()
    if w <= 0 or h <= 0:
        raise ValueError("could not read the video size")
    return int(w), int(h)


def motion_per_sample(src, ffmpeg: str) -> np.ndarray:
    """How much the picture changed since the previous sample (0 for the first), one value per sample."""
    w, h = _dimensions(src)
    height = max(2, int(round(WIDTH * h / w / 2)) * 2)
    frame_bytes = WIDTH * height
    cmd = [ffmpeg, "-v", "error", "-i", str(src), "-an",
           "-vf", f"fps={SAMPLE_FPS},scale={WIDTH}:{height},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    count = len(raw) // frame_bytes
    if count < 2:
        return np.zeros(count)
    frames = np.frombuffer(raw[: count * frame_bytes], dtype=np.uint8).reshape(count, frame_bytes)
    change = np.abs(frames[1:].astype(np.int16) - frames[:-1].astype(np.int16)).mean(axis=1)
    return np.concatenate([[0.0], change])


def loudness_per_sample(src, ffmpeg: str, count: int) -> np.ndarray:
    """Loudness (RMS, 0..1) for each picture sample. All zeros when the video has no audio."""
    cmd = [ffmpeg, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(AUDIO_RATE),
           "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    out = np.zeros(count)
    pcm = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype=np.int16).astype(np.float32) / 32768
    per = int(AUDIO_RATE / SAMPLE_FPS)
    blocks = pcm.size // per
    if blocks == 0:
        return out
    rms = np.sqrt((pcm[: blocks * per].reshape(blocks, per) ** 2).mean(axis=1))
    k = min(blocks, count)
    out[:k] = rms[:k]
    return out


def find_action_picks(src, cfg: dict, ffmpeg: str, duration: float) -> list[dict]:
    """Pick the busiest non-overlapping windows. Returns picks in the same shape as the LLM's."""
    clips = cfg["clips"]
    motion = motion_per_sample(src, ffmpeg)
    count = len(motion)
    if count < 2:
        return []
    loud = loudness_per_sample(src, ffmpeg, count)

    motion_n = np.clip(motion / max(float(np.percentile(motion, 95)), 1e-6), 0, 1)
    loud_n = np.clip(loud / max(float(np.percentile(loud, 95)), 1e-6), 0, 1)
    active = (motion_n >= ACTIVE).astype(float)

    length = min(clips["max_sec"], max(clips["min_sec"], 30.0))
    span = min(count, int(length * SAMPLE_FPS))
    step = max(1, int(STEP_SEC * SAMPLE_FPS))
    candidates = []
    for i0 in range(0, count - span + 1, step):
        i1 = i0 + span
        m, d, s = motion_n[i0:i1].mean(), active[i0:i1].mean(), loud_n[i0:i1].mean()
        candidates.append((0.5 * m + 0.3 * d + 0.2 * s, i0, m, d, s))
    candidates.sort(key=lambda c: c[0], reverse=True)

    total = duration or count / SAMPLE_FPS
    chosen: list[tuple[float, float]] = []
    picks: list[dict] = []
    for score, i0, m, d, s in candidates:
        if len(picks) >= cfg["num_clips"]:
            break
        start = i0 / SAMPLE_FPS
        end = min(total, (i0 + span) / SAMPLE_FPS)
        if end - start < clips["min_sec"]:
            continue
        if any(start < c_end + MIN_GAP_SEC and c_start < end + MIN_GAP_SEC for c_start, c_end in chosen):
            continue
        chosen.append((start, end))
        picks.append({
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "title": f"Action moment {len(picks) + 1}",
            "hook_line": "",
            "reason": (f"Busy picture: strong motion {d:.0%} of the time, "
                       f"loud sound {s:.0%} of the time (no speech in this video)."),
            "score": round(float(score) * 10, 2),
            "score_breakdown": {"motion": round(float(m) * 10, 2), "activity": round(float(d) * 10, 2),
                                "sound": round(float(s) * 10, 2)},
            "selected_by": "action",
        })
    log.info("action picks: %d moments from %d windows (no speech)", len(picks), len(candidates))
    return picks
