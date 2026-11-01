"""Render one raw clip into a finished 9:16 MP4 in a single ffmpeg pass."""

from __future__ import annotations

import subprocess
from pathlib import Path

from clipper.captions import write_ass
from clipper.reframe import crop_geometry, crop_x_expr
from clipper.util import ClipperError

HEAD_ROOM = 0.10   # seconds kept before the first word
TAIL_ROOM = 0.35   # seconds kept after the last word


def trim_window(raw: dict, clip: dict, duration: float) -> tuple[float, float]:
    """(start, end) of the part of the raw file to keep, in raw-file seconds."""
    offset = raw["offset_sec"]  # source time of the raw file's first frame
    return (max(0.0, clip["start_sec"] - HEAD_ROOM - offset),
            min(duration, clip["end_sec"] + TAIL_ROOM - offset))


def render_clip(
    raw: dict, clip: dict, analysis: dict, segments: list[dict], out_path: Path, cfg: dict, ffmpeg: str
) -> dict:
    r = cfg["render"]
    offset = raw["offset_sec"]
    trim_start, trim_end = trim_window(raw, clip, analysis["duration"])
    if trim_end - trim_start < 1:
        raise ClipperError(f"clip {clip['id']}: raw video too short to render")

    crop_w, crop_h, y = crop_geometry(analysis["width"], analysis["height"], r["width"], r["height"])
    x_expr = crop_x_expr(analysis["shots"], analysis["width"], crop_w, t_offset=trim_start)
    vf = [
        "setpts=PTS-STARTPTS",
        f"trim=start={trim_start:.3f}:end={trim_end:.3f}",
        "setpts=PTS-STARTPTS",
        f"crop=w={crop_w}:h={crop_h}:x='{x_expr}':y={y}",
        f"scale={r['width']}:{r['height']}:flags=lanczos",
        "setsar=1",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out_path.with_suffix(".ass").name
    n_words = 0
    if r["captions"]:
        t0 = offset + trim_start
        n_words = write_ass(out_path.with_suffix(".ass"), segments, clip["start_sec"], clip["end_sec"], t0, r)
        # Relative filename + cwd avoids ffmpeg filter-path escaping of Windows drive letters.
        vf.append(f"ass={ass_name}")
    graph = (
        f"[0:v]{','.join(vf)}[v];"
        f"[0:a]asetpts=PTS-STARTPTS,atrim=start={trim_start:.3f}:end={trim_end:.3f},asetpts=PTS-STARTPTS[a]"
    )
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(Path(raw["path"]).resolve()),
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", r["preset"], "-crf", str(r["crf"]), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        out_path.name,
    ]
    proc = subprocess.run(cmd, cwd=out_path.parent, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise ClipperError(f"clip {clip['id']}: ffmpeg failed: {proc.stderr.strip()[-800:]}")

    thumb = out_path.with_suffix(".jpg")
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", out_path.name,
         "-frames:v", "1", "-vf", "scale=360:-2", thumb.name],
        cwd=out_path.parent, capture_output=True,
    )
    return {
        "duration_sec": round(trim_end - trim_start, 3),
        "source_t0": round(offset + trim_start, 3),
        "caption_words": n_words,
        "thumbnail": thumb.name if thumb.is_file() else None,
    }
