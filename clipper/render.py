"""Render one raw clip into a finished 9:16 MP4 in a single ffmpeg pass."""

from __future__ import annotations

import subprocess
from pathlib import Path

from clipper.branding import EDGE_MARGIN, logo_xy
from clipper.caption_style import caption_render
from clipper.captions import write_ass
from clipper.pycaps_captions import OURS, caption_words, clip_caption_template, pycaps_document, render_pycaps
from clipper.reframe import crop_geometry, crop_x_expr, panel_crop, panel_x_expr, split_plan, window_left
from clipper.source_text import find_burned_in
from clipper.util import ClipperError

HEAD_ROOM = 0.10   # seconds kept before the first word
TAIL_ROOM = 0.35   # seconds kept after the last word
NEXT_LINE_GAP = 0.05  # the tail stops this long before the next line's first word


def cut_by_crop(found: dict, shots: list[dict], width: int, crop_w: int) -> bool:
    """True when the 9:16 window would cut burned-in text while that text is on screen."""
    if found["x0"] is None:
        return False
    left, right = found["x0"] * width, found["x1"] * width
    for a, b in found["spans"]:
        for s in shots:
            if s["end"] <= a or s["start"] >= b:
                continue
            x = window_left(s["center_x"], width, crop_w)
            if left < x or right > x + crop_w:
                return True
    return False


def tail_end(clip_end: float, segments: list[dict] | None = None) -> float:
    """Where the clip stops: TAIL_ROOM after its last word, but never after the next line has started."""
    limit = clip_end + TAIL_ROOM
    if segments:
        starts = [s["start_sec"] for s in segments if s["start_sec"] >= clip_end]
        starts += [w["start"] for s in segments for w in s["words"] if w["start"] >= clip_end]
        if starts:
            limit = min(limit, min(starts) - NEXT_LINE_GAP)
    return max(clip_end, limit)


def trim_window(raw: dict, clip: dict, duration: float, segments: list[dict] | None = None) -> tuple[float, float]:
    """(start, end) of the part of the raw file to keep, in raw-file seconds."""
    offset = raw["offset_sec"]  # source time of the raw file's first frame
    return (max(0.0, clip["start_sec"] - HEAD_ROOM - offset),
            min(duration, tail_end(clip["end_sec"], segments) - offset))


def render_clip(
    raw: dict, clip: dict, analysis: dict, segments: list[dict], out_path: Path, cfg: dict, ffmpeg: str
) -> dict:
    r = caption_render(cfg["render"], clip.get("caption_style"))   # the job's settings, with this clip's captions over them
    offset = raw["offset_sec"]
    trim_start, trim_end = trim_window(raw, clip, analysis["duration"], segments)
    if trim_end - trim_start < 1:
        raise ClipperError(f"clip {clip['id']}: raw video too short to render")

    crop_w, crop_h, y = crop_geometry(analysis["width"], analysis["height"], r["width"], r["height"])
    x_expr = crop_x_expr(analysis["shots"], analysis["width"], crop_w, t_offset=trim_start)
    found = {"spans": [], "x0": None, "x1": None}
    if r["captions"] and r.get("source_captions", "auto") == "auto":
        found = find_burned_in(Path(raw["path"]), offset, analysis["width"], analysis["height"], segments, ffmpeg)
    layout = "fit" if cut_by_crop(found, analysis["shots"], analysis["width"], crop_w) else "crop"
    split, pair = split_plan(analysis, trim_start, trim_end - trim_start)
    panel_w, panel_h = panel_crop(analysis["width"], analysis["height"], r["width"], r["height"])
    if panel_w * panel_h == 0 or panel_w < panel_h * 0.5:   # a source too narrow for a half-frame crop
        split = []

    cut = [
        "setpts=PTS-STARTPTS",
        f"trim=start={trim_start:.3f}:end={trim_end:.3f}",
        "setpts=PTS-STARTPTS",
    ]
    if split:
        # Two people talking: the frame is split in two, the left person on top and the right one below.
        head, lead, body = f"[0:v]{','.join(cut)},split=2[bsrc][ssrc];", "[bsrc]", []
    else:
        head, lead, body = "", "[0:v]", cut
    if layout == "crop":
        base = lead + ",".join(body + [
            f"crop=w={crop_w}:h={crop_h}:x='{x_expr}':y={y}",
            f"scale={r['width']}:{r['height']}:flags=lanczos",
            "setsar=1",
        ])
    else:
        # The whole picture, letterboxed over a blurred copy of itself, so text at the sides is not cut off.
        fit_h = round(r["width"] * analysis["height"] / analysis["width"] / 2) * 2
        base = (
            lead + ",".join(body + ["split=2[bg][fg]"]) + ";"
            f"[bg]scale={r['width']}:{r['height']}:force_original_aspect_ratio=increase,"
            f"crop={r['width']}:{r['height']},boxblur=20:2[blur];"
            f"[fg]scale={r['width']}:{fit_h}:flags=lanczos[sharp];"
            f"[blur][sharp]overlay=0:(H-h)/2,setsar=1"
        )
    video = base
    if split:
        half_h = r["height"] // 2
        # Each half follows its person, so whoever is talking stays in frame; the median is the fallback.
        top_x = panel_x_expr(analysis["two"], trim_start, split, analysis["width"], panel_w, 1) \
            or str(window_left(pair[0], analysis["width"], panel_w))
        bot_x = panel_x_expr(analysis["two"], trim_start, split, analysis["width"], panel_w, 2) \
            or str(window_left(pair[1], analysis["width"], panel_w))
        panels = (
            f"[ssrc]split=2[sa][sb];"
            f"[sa]crop=w={panel_w}:h={panel_h}:x='{top_x}':y=0,scale={r['width']}:{half_h}:flags=lanczos,setsar=1[pa];"
            f"[sb]crop=w={panel_w}:h={panel_h}:x='{bot_x}':y=0,scale={r['width']}:{half_h}:flags=lanczos,setsar=1[pb];"
            f"[pa][pb]vstack=inputs=2[sp]"
        )
        enable = "+".join(f"between(t,{a},{b})" for a, b in split)
        video = f"{head}{base}[base];{panels};[base][sp]overlay=enable='{enable}'"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out_path.with_suffix(".ass").name
    n_words = 0
    template = clip_caption_template(clip, cfg)
    # A pycaps style is drawn after this pass (see below); the ASS captions are drawn in this pass.
    pycaps_mode = bool(r["captions"]) and template != OURS
    t0 = offset + trim_start
    # No captions of ours while the picture already shows its own, so the same words never stack.
    skip = [(offset + a, offset + b) for a, b in found["spans"]]
    if r["captions"] and not pycaps_mode:
        n_words = write_ass(out_path.with_suffix(".ass"), segments, clip["start_sec"], clip["end_sec"], t0, r,
                            skip=skip, layout=layout, split=split, edits=clip.get("caption_text"))
        # Relative filename + cwd avoids ffmpeg filter-path escaping of Windows drive letters.
        video += f",ass={ass_name}"
    voice = f"[0:a]asetpts=PTS-STARTPTS,atrim=start={trim_start:.3f}:end={trim_end:.3f},asetpts=PTS-STARTPTS"
    inputs = ["-i", str(Path(raw["path"]).resolve())]
    music = clip.get("music")
    if music:
        # The track loops under the voice, ducks while someone speaks (sidechain), and fades out at the end.
        length = trim_end - trim_start
        inputs += ["-stream_loop", "-1", "-i", str(Path(music["path"]).resolve())]
        audio = (
            f"{voice},asplit=2[va][vk];"
            f"[1:a]atrim=end={length:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=1,"
            f"afade=t=out:st={max(0.0, length - 1.5):.3f}:d=1.5,volume={music['volume'] / 100:.3f}[m];"
            f"[m][vk]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=300[md];"
            f"[va][md]amix=inputs=2:duration=first:normalize=0[a]"
        )
    else:
        audio = f"{voice}[a]"
    # Logo and progress bar go over the finished picture (captions included). Their inputs follow the music's.
    length = f"{trim_end - trim_start:.3f}"
    picture = [f"{video}[v0]"]
    cur = "v0"
    brand = clip.get("branding") or {}
    logo, bar = brand.get("logo"), brand.get("progress")
    if logo:
        li = inputs.count("-i")
        inputs += ["-loop", "1", "-t", length, "-i", str(Path(logo["path"]).resolve())]
        x, y = logo_xy(logo["position"], round(r["width"] * EDGE_MARGIN))
        width = round(r["width"] * logo["size"] / 100) // 2 * 2
        picture += [f"[{li}:v]format=rgba,colorchannelmixer=aa={logo['opacity'] / 100:.2f},scale={width}:-2[lg]",
                    f"[{cur}][lg]overlay=x={x}:y={y}[v1]"]
        cur = "v1"
    if bar:
        bi = inputs.count("-i")
        inputs += ["-loop", "1", "-t", length, "-i", str(Path(bar["path"]).resolve())]
        # The bar is revealed from left to right: a pixel shows while its column is before the clip's progress.
        reveal = f"if(lte(X,W*T/{length}),alpha(X,Y),0)"
        edge_y = "0" if bar["edge"] == "top" else "main_h-overlay_h"
        keep = "r='r(X,Y)':g='g(X,Y)':b='b(X,Y)'"  # colours pass through; only the alpha is set per frame
        picture += [f"[{bi}:v]format=rgba,geq={keep}:a='{reveal}'[pb]", f"[{cur}][pb]overlay=x=0:y={edge_y}[v]"]
    else:
        picture.append(f"[{cur}]null[v]")
    graph = ";".join(picture) + f";{audio}"
    # With a pycaps style the picture is written beside the final file first; pycaps then draws its captions onto it.
    base_path = out_path.with_name(out_path.stem + "_base.mp4") if pycaps_mode else out_path
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", r["preset"], "-crf", str(r["crf"]), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        base_path.name,
    ]
    proc = subprocess.run(cmd, cwd=out_path.parent, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise ClipperError(f"clip {clip['id']}: ffmpeg failed: {proc.stderr.strip()[-800:]}")
    if pycaps_mode:
        words = caption_words(segments, clip["start_sec"], clip["end_sec"], clip.get("caption_text"), skip)
        document = pycaps_document(words, t0, trim_end - trim_start)
        try:
            render_pycaps(base_path.resolve(), out_path.resolve(), document, template, Path(cfg["work_dir"]))
        except Exception as e:  # pycaps drives a browser for the text; say which step failed
            raise ClipperError(f"clip {clip['id']}: captions ({template}) failed: {e}") from e
        finally:
            base_path.unlink(missing_ok=True)
        n_words = sum(len(seg["words"]) for seg in document["segments"])

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
        "music": music["name"] if music else None,
        "branding": {"logo": bool(logo), "progress": bool(bar)},
        "layout": layout,
        "burned_in_sec": round(sum(b - a for a, b in found["spans"]), 1),
        "thumbnail": thumb.name if thumb.is_file() else None,
    }
