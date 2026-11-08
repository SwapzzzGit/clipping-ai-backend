"""Captions already burned into the picture, found with OCR.

A line of on-screen text counts as a caption when most of its words are spoken at that moment,
so a score or a player name on a game HUD is not mistaken for one. Seconds that show such a
caption keep our own captions off (render.py), and the result is cached next to the raw clip.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

from clipper.util import log, read_json, write_json

SAMPLE_FPS = 1.0   # one picture per second: a caption stays on screen for longer than that
WIDTH = 640        # OCR on a small picture; burned-in captions are large
MIN_CONF = 0.6     # OCR confidence needed to trust a text line
MATCH = 0.5        # share of a line's words that must be spoken within MATCH_SEC of the picture
MATCH_SEC = 2.0
_WORD = re.compile(r"[a-z0-9']+")

_engine = None
_engine_missing = False


def _ocr_engine():
    global _engine, _engine_missing
    if _engine is None and not _engine_missing:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            _engine_missing = True
            log.warning("rapidocr-onnxruntime is not installed: captions already in the picture are not detected")
        else:
            _engine = RapidOCR()
    return _engine


def sample_pictures(src: Path, ffmpeg: str, width: int, height: int) -> np.ndarray:
    """One small BGR picture per second of the file, as an array of shape (seconds, h, w, 3)."""
    w = WIDTH
    h = max(2, round(WIDTH * height / width / 2) * 2)
    cmd = [ffmpeg, "-v", "error", "-i", str(src), "-an", "-vf", f"fps={SAMPLE_FPS},scale={w}:{h}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (w * h * 3)
    return np.frombuffer(raw[: n * w * h * 3], dtype=np.uint8).reshape(n, h, w, 3)


def read_picture(picture: np.ndarray) -> list[dict]:
    """The text lines on one picture: {"words": [...], "x0", "x1"} with x as a share of the width."""
    engine = _ocr_engine()
    if engine is None:
        return []
    found, _ = engine(picture)
    width = picture.shape[1]
    lines = []
    for box, text, conf in found or []:
        words = _WORD.findall(text.lower())
        if float(conf) >= MIN_CONF and words:
            xs = [float(p[0]) for p in box]
            lines.append({"words": words, "x0": min(xs) / width, "x1": max(xs) / width})
    return lines


def spoken_words(segments: list[dict], t: float) -> set[str]:
    """Words spoken within MATCH_SEC of source time `t`."""
    out: set[str] = set()
    for s in segments:
        if s["end_sec"] >= t - MATCH_SEC and s["start_sec"] <= t + MATCH_SEC:
            out.update(_WORD.findall(s["text"].lower()))
    return out


def judge(pictures: list[list[dict]], offset: float, segments: list[dict]) -> dict:
    """Which seconds show a burned-in caption.

    `pictures[i]` is the text read from second i of the raw clip, whose first frame is source
    time `offset`. Returns spans in raw-clip seconds and how far the caption text reaches
    across the picture (x0..x1, shares of the width).
    """
    spans: list[list[float]] = []
    reach: list[tuple[float, float]] = []
    for i, lines in enumerate(pictures):
        spoken = spoken_words(segments, offset + i / SAMPLE_FPS)
        caption = [ln for ln in lines
                   if len(ln["words"]) >= 2 and sum(w in spoken for w in ln["words"]) / len(ln["words"]) >= MATCH]
        if not caption:
            continue
        start, end = i / SAMPLE_FPS, (i + 1) / SAMPLE_FPS
        if spans and spans[-1][1] == start:
            spans[-1][1] = end
        else:
            spans.append([start, end])
        reach += [(ln["x0"], ln["x1"]) for ln in caption]
    if not reach:
        return {"spans": [], "x0": None, "x1": None}
    return {"spans": spans, "x0": round(min(r[0] for r in reach), 4), "x1": round(max(r[1] for r in reach), 4)}


def find_burned_in(src: Path, offset: float, width: int, height: int, segments: list[dict],
                   ffmpeg: str) -> dict:
    """Burned-in caption seconds for one raw clip, cached as <clip>.source_text.json."""
    cache = src.with_suffix(".source_text.json")
    if cache.is_file():
        return read_json(cache)
    empty = {"spans": [], "x0": None, "x1": None}
    if not any(s["text"].strip() for s in segments) or _ocr_engine() is None:
        return empty  # no speech to match against, or no OCR engine: nothing to find
    pictures = sample_pictures(src, ffmpeg, width, height)
    found = judge([read_picture(p) for p in pictures], offset, segments)
    write_json(cache, found)
    seconds = sum(b - a for a, b in found["spans"])
    log.info("burned-in captions on screen for %.0f of %d s", seconds, len(pictures))
    return found
