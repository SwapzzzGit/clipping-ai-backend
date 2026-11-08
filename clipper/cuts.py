"""Text-based cuts: the words a reviewer deletes are cut out of the finished clip.

Captions and the reframed picture are already burned into the clip, so they move with a cut on their own;
only the timeline has to close the gap. Cuts are kept in source seconds, so a later re-render can apply the
same cuts again (or drop them, for a restore) to a fresh render.
"""

from __future__ import annotations

import array
import json
import re
import subprocess
from pathlib import Path

from clipper.util import ClipperError, has_audio

MIN_KEPT_SEC = 3.0
_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")


def keep_ranges(duration: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of [0, duration] that survive the cuts, in order."""
    merged: list[list[float]] = []
    for a, b in sorted((max(0.0, a), min(duration, b)) for a, b in cuts if b > a):
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    keep: list[tuple[float, float]] = []
    pos = 0.0
    for a, b in merged:
        if a > pos + 1e-6:
            keep.append((pos, a))
        pos = b
    if pos < duration - 1e-6:
        keep.append((pos, duration))
    return keep


def probe_duration(ffmpeg: str, path: Path) -> float:
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace")
    m = _DURATION.search(proc.stderr)
    if not m:
        raise ClipperError(f"could not read the length of {path.name}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def cut_out(ffmpeg: str, src: Path, dst: Path, keep: list[tuple[float, float]], render: dict) -> None:
    """Re-encode src with only the `keep` ranges, joined end to end. Frame-exact: trims run on decoded frames."""
    audio = has_audio(ffmpeg, src)
    parts: list[str] = []
    pairs: list[str] = []
    for i, (a, b) in enumerate(keep):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
            pairs.append(f"[v{i}][a{i}]")
        else:
            pairs.append(f"[v{i}]")
    n = len(keep)
    if audio:
        parts.append("".join(pairs) + f"concat=n={n}:v=1:a=1[outv][outa]")
        maps = ["-map", "[outv]", "-map", "[outa]", "-c:a", "aac", "-b:a", "160k"]
    else:
        parts.append("".join(pairs) + f"concat=n={n}:v=1:a=0[outv]")
        maps = ["-map", "[outv]"]
    cmd = [ffmpeg, "-v", "error", "-y", "-i", src.name, "-filter_complex", ";".join(parts), *maps,
           "-c:v", "libx264", "-preset", render["preset"], "-crf", str(render["crf"]), "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", dst.name]
    proc = subprocess.run(cmd, cwd=src.parent, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise ClipperError(f"cutting words failed: {proc.stderr.strip()[-400:]}")


def apply_cuts(ffmpeg: str, out: Path, rendered: dict, cuts: list[dict], render: dict) -> dict:
    """Cut the reviewer's words out of a freshly rendered clip. Cut times are source seconds; the clip's
    picture starts at source_t0."""
    t0, dur = rendered["source_t0"], rendered["duration_sec"]
    keep = keep_ranges(dur, [(c["s"] - t0, c["e"] - t0) for c in cuts])
    kept = sum(b - a for a, b in keep)
    if kept < MIN_KEPT_SEC:
        raise ClipperError(f"the cut would leave only {kept:.1f} s of this clip")
    tmp = out.with_name(out.stem + ".cut.mp4")
    cut_out(ffmpeg, out, tmp, keep, render)
    tmp.replace(out)
    baked = [{"s": c["s"], "e": c["e"], "text": c.get("text", "")} for c in cuts]
    return {**rendered, "duration_sec": round(kept, 3), "cuts_baked": baked}


def waveform(ffmpeg: str, path: Path, step: float = 0.1) -> dict:
    """Peak level (0-1) of a clip's sound every `step` seconds. Cached beside the clip; refreshed when it changes."""
    cache = path.with_name(path.stem + ".wave.json")
    stamp = path.stat().st_mtime_ns
    if cache.is_file():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("stamp") == stamp:
            return {"step": data["step"], "peaks": data["peaks"]}
    rate = 8000
    proc = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate),
                           "-f", "s16le", "-"], capture_output=True)
    if proc.returncode != 0:
        raise ClipperError("could not read the sound of this clip")
    raw = proc.stdout[: len(proc.stdout) - len(proc.stdout) % 2]
    samples = array.array("h")
    samples.frombytes(raw)
    per = int(rate * step)
    peaks = [max((abs(v) for v in samples[i:i + per]), default=0) for i in range(0, len(samples), per)]
    top = max(peaks, default=0) or 1
    data = {"step": step, "peaks": [round(p / top, 3) for p in peaks], "stamp": stamp}
    cache.write_text(json.dumps(data), encoding="utf-8")
    return {"step": step, "peaks": data["peaks"]}
