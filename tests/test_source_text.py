from pathlib import Path

import numpy as np
import pytest

from clipper.captions import build_ass, write_ass
from clipper.render import cut_by_crop
from clipper.source_text import find_burned_in, judge

STYLE = {"width": 1080, "height": 1920, "caption_font": "Arial", "caption_font_size": 84,
         "caption_highlight": "&H0000E5FF", "caption_words_per_line": 3}
SPEECH = [
    {"start_sec": 0.0, "end_sec": 3.0, "text": "we are going to win this", "words": []},
    {"start_sec": 10.0, "end_sec": 12.0, "text": "totally different talk here", "words": []},
]


def line(text, x0=0.2, x1=0.8):
    return {"words": text.split(), "x0": x0, "x1": x1}


def test_judge_marks_only_seconds_where_on_screen_text_is_spoken():
    pictures = [[line("we are going")], [], [line("to win this")], [], [], [], [], [], [], [], [line("score 42")], []]
    found = judge(pictures, 0.0, SPEECH)
    assert found["spans"] == [[0.0, 1.0], [2.0, 3.0]]  # the HUD "score 42" is not a caption
    assert found["x0"] == 0.2 and found["x1"] == 0.8


def test_judge_uses_source_time_for_the_clip_offset():
    shifted = [{**s, "start_sec": s["start_sec"] + 100, "end_sec": s["end_sec"] + 100} for s in SPEECH]
    found = judge([[line("we are going")]], 100.0, shifted)
    assert found["spans"] == [[0.0, 1.0]]


def test_no_speech_or_no_text_finds_nothing():
    assert judge([[line("we are going")]], 0.0, [])["spans"] == []
    assert judge([[]], 0.0, SPEECH) == {"spans": [], "x0": None, "x1": None}


def test_find_burned_in_skips_ocr_when_there_is_no_speech(tmp_path):
    src = tmp_path / "clip.mp4"
    assert find_burned_in(src, 0.0, 1920, 1080, [], "ffmpeg") == {"spans": [], "x0": None, "x1": None}


def test_cut_by_crop_when_text_is_wider_than_the_9x16_window():
    shots = [{"start": 0.0, "end": 10.0, "center_x": 0.5}]
    crop_w = 607  # 9:16 window of a 1920x1080 picture
    wide = {"spans": [[0.0, 2.0]], "x0": 0.1, "x1": 0.9}
    narrow = {"spans": [[0.0, 2.0]], "x0": 0.45, "x1": 0.55}
    assert cut_by_crop(wide, shots, 1920, crop_w) is True
    assert cut_by_crop(narrow, shots, 1920, crop_w) is False
    assert cut_by_crop({"spans": [], "x0": None, "x1": None}, shots, 1920, crop_w) is False


def test_cut_by_crop_checks_the_window_where_the_text_is_on_screen():
    # The speaker is at the far left for the first 2 s, so a centred caption is cut then.
    shots = [{"start": 0.0, "end": 2.0, "center_x": 0.1}, {"start": 2.0, "end": 10.0, "center_x": 0.5}]
    found = {"spans": [[0.0, 1.0]], "x0": 0.45, "x1": 0.55}
    assert cut_by_crop(found, shots, 1920, 607) is True
    assert cut_by_crop({**found, "spans": [[5.0, 6.0]]}, shots, 1920, 607) is False


def test_our_captions_skip_the_seconds_with_burned_in_captions(tmp_path):
    segs = [{"start_sec": 1.0, "end_sec": 1.5, "text": "hello", "words": [{"start": 1.0, "end": 1.5, "text": "hello"}]},
            {"start_sec": 5.0, "end_sec": 5.5, "text": "world", "words": [{"start": 5.0, "end": 5.5, "text": "world"}]}]
    path = tmp_path / "clip.ass"
    n = write_ass(path, segs, 0.0, 10.0, 0.0, STYLE, skip=[(4.5, 6.0)], layout="crop")
    text = path.read_text(encoding="utf-8")
    assert n == 1 and "hello" in text and "world" not in text


def test_fit_layout_puts_captions_in_the_top_band():
    assert ",8,80,80,230,1" in build_ass([], 0.0, 10.0, STYLE, layout="fit")
    assert ",2,80,80,518,1" in build_ass([], 0.0, 10.0, STYLE, layout="crop")


def test_ocr_reads_a_caption_drawn_on_a_picture():
    pytest.importorskip("rapidocr_onnxruntime")
    import cv2

    from clipper.source_text import read_picture

    picture = np.full((360, 640, 3), 255, np.uint8)
    cv2.putText(picture, "HELLO WORLD", (60, 220), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 4)
    words = [w for ln in read_picture(picture) for w in ln["words"]]
    assert "hello" in words and "world" in words
