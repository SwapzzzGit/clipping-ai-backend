"""Small shared helpers: logging, JSON cache files, ffmpeg lookup, time formatting."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("clipper")


class ClipperError(RuntimeError):
    """A fatal, user-facing error (printed without a traceback)."""


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    """Write atomically so an interrupted run never leaves a half-written cache file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_warned_ffmpeg = False


def find_ffmpeg(configured: str | None = None, cache_dir: str | Path = "work") -> str:
    if configured:
        if Path(configured).is_file() or shutil.which(configured):
            return configured
        raise ClipperError(f"ffmpeg_path {configured!r} not found")
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        exe = None
    if exe:
        # yt-dlp only recognises a binary literally named ffmpeg(.exe), so expose one.
        shim = Path(cache_dir) / ".bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not shim.is_file() or shim.stat().st_size != exe.stat().st_size:
            shim.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exe, shim)
        global _warned_ffmpeg
        if not _warned_ffmpeg:
            _warned_ffmpeg = True
            log.warning("ffmpeg not on PATH; using bundled imageio-ffmpeg (no ffprobe). Installing ffmpeg is recommended.")
        return str(shim.resolve())
    raise ClipperError("ffmpeg not found. Install it and put it on PATH, or set ffmpeg_path in config.yaml.")


def has_audio(ffmpeg: str, path: str | Path) -> bool:
    """True if the file has an audio stream (parsed from `ffmpeg -i`, so ffprobe is not needed)."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace")
    return "Audio:" in proc.stderr


def js_runtime() -> str | None:
    """yt-dlp needs a JS runtime for YouTube challenges; it defaults to deno, so fall back to node."""
    if shutil.which("deno"):
        return "deno"
    return "node" if shutil.which("node") else None


def hms(seconds: float) -> str:
    """Seconds -> HH:MM:SS.mmm (yt-dlp/ffmpeg style)."""
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "clip"


def extract_json(text: str) -> Any:
    """Parse JSON from an LLM reply, tolerating ```json fences and leading/trailing prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON object in LLM reply")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start : end + 1])
