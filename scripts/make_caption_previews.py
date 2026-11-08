"""Render one still frame per pycaps caption style, so the editor can show what each style looks like.

    python scripts/make_caption_previews.py --source path/to/any_9x16_clip.mp4

Writes assets/caption-previews/<style>.jpg. Run it again after a pycaps upgrade.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipper.pycaps_captions import PREVIEW_TEXT, pycaps_document, render_pycaps, template_names  # noqa: E402
from clipper.util import find_ffmpeg  # noqa: E402

SAMPLE_SECONDS = 3.0
FRAME_AT = 2.0


def sample_words() -> list[dict]:
    words, t = [], 0.4
    for text in PREVIEW_TEXT.split():
        words.append({"text": text, "start": round(t, 2), "end": round(t + 0.42, 2)})
        t += 0.46
    return words


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="a 9:16 clip to draw the previews on")
    ap.add_argument("--out", default=str(ROOT / "assets" / "caption-previews"))
    ap.add_argument("--work", default=str(ROOT / "work"))
    ap.add_argument("--ffmpeg", default=None)
    args = ap.parse_args()

    ffmpeg = find_ffmpeg(args.ffmpeg, cache_dir=args.work)
    out_dir, work = Path(args.out), Path(args.work) / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    sample = work / "sample.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", str(Path(args.source).resolve()),
                    "-t", str(SAMPLE_SECONDS), "-vf", "scale=1080:1920,setsar=1", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-c:a", "aac", str(sample)], check=True)
    document = pycaps_document(sample_words(), 0.0, SAMPLE_SECONDS)

    for name in template_names():
        video = work / f"{name}.mp4"
        print(f"rendering {name} ...", flush=True)
        render_pycaps(sample.resolve(), video.resolve(), document, name, work)
        frame = out_dir / f"{name}.jpg"
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(FRAME_AT), "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=360:-2", str(frame)], check=True)
        video.unlink(missing_ok=True)
        print(f"  wrote {frame}", flush=True)


if __name__ == "__main__":
    main()
