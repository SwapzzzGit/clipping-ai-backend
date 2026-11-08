import subprocess

import cv2
import pytest

from clipper.captions import build_ass
from clipper.config import load_config
from clipper.reframe import panel_x_expr, split_plan, window_left
from clipper.render import render_clip
from clipper.util import ClipperError, find_ffmpeg

STYLE = {"width": 1080, "height": 1920, "caption_font": "Arial", "caption_font_size": 60,
         "caption_highlight": "&H0000FFFF", "caption_words_per_line": 3}


def two_people(start, end, xl=0.3, xr=0.7, period=1 / 3):
    """Analysis rows for the frames in which two faces are seen, from `start` to `end` seconds."""
    return [[round(start + i * period, 3), xl, xr] for i in range(round((end - start) / period))]


def test_split_plan_follows_the_stretch_where_two_people_are_on_screen():
    spans, pair = split_plan({"two": two_people(0, 6), "sample_period": 1 / 3}, trim_start=0.0, length=12.0)
    assert len(spans) == 1 and spans[0][0] <= 1.0 and 5.0 <= spans[0][1] <= 6.5
    assert pair == (0.3, 0.7)


def test_split_plan_counts_from_the_clip_start():
    spans, _ = split_plan({"two": two_people(0, 6), "sample_period": 1 / 3}, trim_start=2.0, length=8.0)
    assert spans[0][0] <= 1.0 and 3.5 <= spans[0][1] <= 4.5


def test_each_panel_follows_its_person_over_time():
    # The left person sits at 0.2 for the first 3 s, then moves to 0.4; the right one stays at 0.8.
    two = [[round(i / 3, 3), 0.2 if i < 9 else 0.4, 0.8] for i in range(18)]
    left = panel_x_expr(two, 0.0, [(0.0, 6.0)], 1920, 1215, 1)
    right = panel_x_expr(two, 0.0, [(0.0, 6.0)], 1920, 1215, 2)
    assert "if(lt(t,3.000)," in left and "(t-" in left   # a gradual move, not a jump
    assert f"{window_left(0.2, 1920, 1215):.1f}" in left and f"{window_left(0.4, 1920, 1215):.1f}" in left
    assert f"{window_left(0.8, 1920, 1215):.1f}" in right
    assert panel_x_expr([], 0.0, [(0.0, 6.0)], 1920, 1215, 1) == ""


def test_no_two_people_means_no_split():
    assert split_plan({"two": [], "sample_period": 1 / 3}, 0.0, 5.0) == ([], None)


def test_captions_move_to_the_seam_only_inside_split_spans():
    words = [{"start": 1.0, "end": 1.5, "text": "Hello"}, {"start": 2.5, "end": 3.0, "text": "there"}]
    ass = build_ass(words, t0=0.0, duration=10.0, r=STYLE, split=[(2.0, 4.0)])
    hello = next(line for line in ass.splitlines() if line.startswith("Dialogue") and "Hello" in line)
    there = next(line for line in ass.splitlines() if line.startswith("Dialogue") and "there" in line)
    assert "\\an5\\pos(540,960)" not in hello
    assert "\\an5\\pos(540,960)" in there


def test_split_render_gives_a_full_9_16_clip(tmp_path):
    try:
        ffmpeg = find_ffmpeg(None, tmp_path)
    except ClipperError:
        pytest.skip("ffmpeg is not available")
    raw = tmp_path / "raw.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
                    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "6", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(raw)], check=True)
    analysis = {"width": 1920, "height": 1080, "duration": 6.0, "sample_period": 1 / 3,
                "shots": [{"start": 0.0, "end": 6.0, "center_x": 0.5, "face_ratio": 1.0}],
                "two": two_people(0, 6)}
    cfg = load_config()
    cfg["render"]["captions"] = False
    out = tmp_path / "clip.mp4"
    render_clip({"path": str(raw), "offset_sec": 0.0}, {"id": 1, "title": "t", "start_sec": 1.0, "end_sec": 5.0,
                 "music": None, "branding": None}, analysis, [], out, cfg, ffmpeg)
    cap = cv2.VideoCapture(str(out))
    assert (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))) == (1080, 1920)
