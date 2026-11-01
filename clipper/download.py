"""Step 3: download only the chosen time ranges (never the full video)."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2

from clipper.render import HEAD_ROOM, TAIL_ROOM
from clipper.util import has_audio, hms, js_runtime, log

RAW_NAME = re.compile(r"clip_(\d+)_(\d+)_(\d+)\.mp4")


def is_valid_video(path: Path, ffmpeg: str, min_sec: float = 1.0) -> bool:
    """Readable video of at least `min_sec` that also has an audio track."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if not (cap.isOpened() and fps > 0 and frames / fps >= min_sec):
            return False
    finally:
        cap.release()
    return has_audio(ffmpeg, path)


def find_covering_raw(raw_dir: Path, clip: dict, ffmpeg: str) -> dict | None:
    """An earlier download of this clip that already holds everything render needs (e.g. after a trim)."""
    need_start, need_end = clip["start_sec"] - HEAD_ROOM, clip["end_sec"] + TAIL_ROOM
    for f in sorted(raw_dir.glob(f"clip_{clip['id']:02d}_*.mp4")):
        m = RAW_NAME.fullmatch(f.name)
        if not m or int(m[1]) != clip["id"]:
            continue
        start, end = int(m[2]) / 1000, int(m[3]) / 1000
        if start <= need_start and end >= need_end and is_valid_video(f, ffmpeg):
            return {"path": str(f), "offset_sec": start, "end_sec": end}
    return None


def download_clip(url: str, clip: dict, raw_dir: Path, cfg: dict, ffmpeg: str, duration: float) -> dict:
    """Download [start-pad, end+pad] of one clip. Returns {path, offset_sec, ...}."""
    d = cfg["download"]
    start = max(0.0, clip["start_sec"] - d["padding_sec"])
    end = min(duration or float("inf"), clip["end_sec"] + d["padding_sec"])
    stem = f"clip_{clip['id']:02d}_{int(start * 1000)}_{int(end * 1000)}"
    out = raw_dir / f"{stem}.mp4"
    result = {"path": str(out), "offset_sec": start, "end_sec": end}
    if is_valid_video(out, ffmpeg):
        log.info("clip %d: raw download cached", clip["id"])
        return result
    covering = find_covering_raw(raw_dir, clip, ffmpeg)
    if covering:
        log.info("clip %d: reusing raw download %s", clip["id"], Path(covering["path"]).name)
        return covering

    h = d["max_height"]
    # Subprocess rather than the yt_dlp API: the API does not pass ffmpeg_location on to
    # the section downloader, so --download-sections fails unless ffmpeg is on PATH.
    cmd = [
        sys.executable, "-m", "yt_dlp", url,
        "--no-playlist", "--quiet", "--no-warnings", "--no-progress", "--force-overwrites",
        "-f", f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
        "--merge-output-format", "mp4",
        "--download-sections", f"*{hms(start)}-{hms(end)}",
        "--force-keyframes-at-cuts",
        "--ffmpeg-location", ffmpeg,
        "-o", str(raw_dir / f"{stem}.%(ext)s"),
    ]
    if js_runtime():
        cmd += ["--js-runtimes", js_runtime()]
    last_error = None
    for attempt in range(d["retries"] + 1):
        log.info("clip %d: downloading %s-%s (attempt %d)", clip["id"], hms(start), hms(end), attempt + 1)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0 and is_valid_video(out, ffmpeg):
            return result
        last_error = (proc.stderr or proc.stdout).strip()[-500:] or "downloaded file is missing, unreadable or has no audio"
        log.warning("clip %d: download failed: %s", clip["id"], last_error)
        time.sleep(2 * (attempt + 1))
    return {**result, "error": last_error}


def download_clips(url: str, clips: list[dict], work: Path, cfg: dict, ffmpeg: str, duration: float) -> dict[int, dict]:
    raw_dir = work / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, cfg["download"]["parallel"])) as pool:
        futures = {c["id"]: pool.submit(download_clip, url, c, raw_dir, cfg, ffmpeg, duration) for c in clips}
        return {cid: f.result() for cid, f in futures.items()}
