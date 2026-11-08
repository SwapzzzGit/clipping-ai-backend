"""Thumbnails for the editor's video lane: one picture every few seconds, made once per rendered file.

The pictures sit beside the clip as <stem>_strip_001.jpg, 002, ... and the timing is kept in <stem>_strip.json.
A new render has a new file name, so the thumbnails made for an older file are never reused for it.
"""

import json
import subprocess
from pathlib import Path

from clipper.util import ClipperError

MAX_FRAMES = 60      # enough to fill a timeline at the default zoom; longer clips get a wider interval
STRIP_HEIGHT = 56    # pixels; the lane is about this tall


def filmstrip(ffmpeg: str, src: Path, duration: float) -> dict:
    """Return {"interval", "count"} for the pictures of `src`, making them first if they are missing."""
    interval = max(1.0, round(duration / MAX_FRAMES, 2))
    folder, stem = src.parent, src.stem
    meta_path = folder / f"{stem}_strip.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("interval") == interval and all(
            (folder / f"{stem}_strip_{i:03d}.jpg").is_file() for i in range(1, meta.get("count", 0) + 1)
        ):
            return meta
    for old in folder.glob(f"{stem}_strip_*.jpg"):
        old.unlink()
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-i", str(src),
         "-vf", f"fps=1/{interval},scale=-2:{STRIP_HEIGHT}", "-q:v", "5", str(folder / f"{stem}_strip_%03d.jpg")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ClipperError(f"Could not make the video thumbnails: {proc.stderr.strip()[-200:]}")
    meta = {"interval": interval, "count": len(list(folder.glob(f"{stem}_strip_*.jpg")))}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return meta
