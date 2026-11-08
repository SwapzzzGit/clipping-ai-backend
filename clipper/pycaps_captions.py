"""Captions drawn with pycaps templates (CSS styles from the pycaps project).

The words and their timings come from our transcript, so the editor's corrections and cuts still apply.
The look comes from a pycaps template. `OURS` keeps the burned-in ASS captions from clipper/captions.py.
"""

from __future__ import annotations

import importlib.resources as resources
import json
import os
import shutil
import time
from pathlib import Path

from clipper.captions import apply_caption_edits, clip_words, group_words

OURS = "ours"            # the ASS captions with the job's own caption settings (clipper/captions.py)
DEFAULT_TEMPLATE = "hype"
PREVIEW_DIR = Path(__file__).resolve().parents[1] / "assets" / "caption-previews"  # made by scripts/make_caption_previews.py
EXCLUDED = {"model"}     # pycaps' template for writing templates, not a caption style
PREVIEW_TEXT = "Negative thinking is a major problem"

LABELS = {
    "classic": "Classic",
    "default": "Default",
    "explosive": "Explosive",
    "fast": "Fast",
    "hype": "Hype",
    "line-focus": "Line focus",
    "minimalist": "Minimalist",
    "neo-minimal": "Neo minimal",
    "retro-gaming": "Retro gaming",
    "vibrant": "Vibrant",
    "word-focus": "Word focus",
}
DESCRIPTIONS = {
    "classic": "Plain subtitles, no animation.",
    "default": "The pycaps default look with a simple word animation.",
    "explosive": "Bold words that pop in. Its sound effect is left out.",
    "fast": "Short, quick captions that change fast.",
    "hype": "Uppercase words, the spoken word highlighted in yellow.",
    "line-focus": "The current line is in focus, the rest is muted.",
    "minimalist": "Clean white text with a soft shadow.",
    "neo-minimal": "Minimal text with a modern feel.",
    "retro-gaming": "Pixel font in a retro game style.",
    "vibrant": "Colourful words with energetic highlights.",
    "word-focus": "One word at a time, large and centred.",
}


def template_names() -> list[str]:
    """The built-in pycaps caption styles, sorted by name."""
    from pycaps.template.constants import BUILTIN_TEMPLATES_PACKAGE, CONFIG_FILE_NAME

    root = resources.files(BUILTIN_TEMPLATES_PACKAGE)
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and p.name not in EXCLUDED and p.joinpath(CONFIG_FILE_NAME).is_file())


def is_valid(name: str) -> bool:
    return name == OURS or name in template_names()


def style_options() -> list[dict]:
    """Every caption style the editor can offer: ours first, then each pycaps template."""
    options = [{"name": OURS, "label": "Ours", "description": "Our ASS captions with the job's caption settings.",
                "preview": None}]
    for name in template_names():
        options.append({"name": name, "label": LABELS.get(name, name.title()),
                        "description": DESCRIPTIONS.get(name, ""), "preview": f"/api/caption-previews/{name}"})
    return options


def clip_caption_template(clip: dict, cfg: dict) -> str:
    """The style a clip is captioned with: its own choice, else the job's default."""
    return clip.get("caption_template") or cfg["render"].get("caption_template") or DEFAULT_TEMPLATE


def caption_words(segments: list[dict], start: float, end: float, edits: dict | None = None,
                  skip: list[tuple[float, float]] | tuple = ()) -> list[dict]:
    """The clip's caption words in source seconds, corrected, without the spans the picture already shows."""
    words = apply_caption_edits(clip_words(segments, start, end), edits)
    return [w for w in words if not any(a <= w["start"] < b for a, b in skip)]


def pycaps_document(words: list[dict], t0: float, duration: float) -> dict:
    """pycaps' own transcript format: segments of words, in seconds from the start of the rendered clip.
    `t0` is the source time that maps to 0 in the rendered clip (see clipper/captions.py)."""
    timed: list[dict] = []
    for w in words:
        start = max(0.0, w["start"] - t0)
        end = min(duration, w["end"] - t0)
        if timed and start < timed[-1]["end"]:
            start = timed[-1]["end"]  # words stay in order and never overlap
        if start >= duration or end <= start:
            continue
        timed.append({"text": w["text"], "start": round(start, 3), "end": round(end, 3)})
    segments = [{"start": group[0]["start"], "end": group[-1]["end"], "words": group}
                for group in group_words(timed, per_line=8)]
    return {"segments": segments}


def prepared_template(name: str, work_dir: Path):
    """A copy of a built-in template, ready to use. The `explosive` sound effect is left out, so a clip
    never gets a sound the reviewer did not ask for."""
    from pycaps.template.constants import BUILTIN_TEMPLATES_PACKAGE, CONFIG_FILE_NAME
    from pycaps.template.template import Template

    if name not in template_names():
        raise ValueError(f"Unknown caption style: {name}")
    src = resources.files(BUILTIN_TEMPLATES_PACKAGE).joinpath(name)
    dst = Path(work_dir) / "caption-templates" / name
    config = dst / CONFIG_FILE_NAME
    if not config.is_file():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(str(src), str(dst), ignore=shutil.ignore_patterns("__pycache__"))
        data = json.loads(config.read_text(encoding="utf-8"))
        if data.pop("sound_effects", None) is not None:
            config.write_text(json.dumps(data, indent=2), encoding="utf-8")

    class FolderTemplate(Template):
        def get_json_path(self) -> str:
            return str(config)

    return FolderTemplate(name)


def render_pycaps(source: Path, out: Path, document: dict, template: str, work_dir: Path) -> None:
    """Burn the document's words into `source` with the template, writing `out`."""
    from pycaps import TemplateLoader
    from pycaps.common import VideoQuality
    from pycaps.transcriber import TranscriptFormat

    # pycaps refuses to write over an existing file, so it writes beside `out` and the result replaces `out` only
    # when the render has finished: a failed render leaves the clip's previous file in place.
    tmp = out.with_name(out.stem + ".captioning.mp4")
    tmp.unlink(missing_ok=True)
    builder = TemplateLoader(prepared_template(template, work_dir)).with_input_video(str(source)).load(False)
    builder.with_transcription(document, format=TranscriptFormat.PYCAPS_JSON)
    builder.with_output_video(str(tmp))
    builder.with_video_quality(VideoQuality.HIGH)
    builder.should_save_subtitle_data(False)
    try:
        builder.build().run()
        _replace_when_free(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def _replace_when_free(tmp: Path, out: Path, tries: int = 40, pause: float = 1.5) -> None:
    """Swap the finished render in. On Windows a file that a video player or the file server still has open
    cannot be replaced, so wait a little for it to be let go, then say what to do."""
    for attempt in range(tries):
        try:
            os.replace(tmp, out)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise RuntimeError(f"{out.name} is open in another program (a video player or preview). "
                                   "Close it and re-render this clip.")
            time.sleep(pause)
