import pytest

from clipper.captions import build_ass, clip_words, group_words
from clipper.config import DEFAULTS, load_campaign_rules, load_config
from clipper.pipeline import build_manifest
from clipper.reframe import _merge_short_shots, crop_geometry, crop_x_expr
from clipper.render import trim_window
from tests.conftest import make_segments


def test_crop_geometry_landscape_and_portrait():
    assert crop_geometry(1920, 1080, 1080, 1920) == (606, 1080, 0)
    w, h, y = crop_geometry(720, 1280, 1080, 1920)  # already 9:16
    assert (w, h, y) == (720, 1280, 0)
    w, h, y = crop_geometry(1000, 1000, 1080, 1920)  # square: full height, centred
    assert h == 1000 and w == 562 and y == 0


def test_crop_x_expr_is_piecewise_and_clamped():
    shots = [{"start": 0, "end": 3, "center_x": 0.0}, {"start": 3, "end": 6, "center_x": 0.5},
             {"start": 6, "end": 9, "center_x": None}, {"start": 9, "end": 12, "center_x": 1.0}]
    expr = crop_x_expr(shots, width=1920, crop_w=606, t_offset=1.0)
    assert expr == "if(lt(t,2.000),0,if(lt(t,5.000),657,if(lt(t,8.000),657,1314)))"
    assert crop_x_expr([], 1920, 606) == "657"


def test_short_flash_shots_are_merged():
    shots = [{"start": 0, "end": 5, "center_x": 0.3}, {"start": 5, "end": 5.2, "center_x": 0.8},
             {"start": 5.2, "end": 9, "center_x": 0.6}]
    merged = _merge_short_shots(shots)
    assert [(s["start"], s["end"]) for s in merged] == [(0, 5.2), (5.2, 9)]


def test_caption_groups_break_on_sentences_and_size():
    segs = make_segments(["one two three four five.", "six seven."])
    words = clip_words(segs, 0, 100)
    groups = [[w["text"] for w in g] for g in group_words(words, 3)]
    assert groups == [["one", "two", "three"], ["four", "five."], ["six", "seven."]]


def test_clip_words_spreads_untimed_segments():
    segs = [{"start_sec": 10, "end_sec": 12, "text": "a b c d", "words": []}]
    words = clip_words(segs, 10, 12)
    assert [w["start"] for w in words] == [10, 10.5, 11, 11.5]


def test_ass_has_highlighted_word_events_relative_to_clip():
    segs = make_segments(["hello there {friend}"], start=100)
    words = clip_words(segs, 100, 102)
    ass = build_ass(words, t0=99.5, duration=3, r=DEFAULTS["render"])
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 3
    assert events[0].startswith("Dialogue: 0,0:00:00.50,0:00:00.90,")
    assert "{\\c&H0000E5FF}hello{\\c&H00FFFFFF&} there friend" in events[0]
    assert "{friend}" not in ass  # braces would be parsed as ASS override tags


def test_config_merge_and_rules(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("num_clips: 3\nllm:\n  provider: mock\n", encoding="utf-8")
    cfg = load_config(p, {"clips": {"max_sec": 45}})
    assert cfg["num_clips"] == 3 and cfg["llm"]["provider"] == "mock"
    assert cfg["llm"]["concurrency"] == DEFAULTS["llm"]["concurrency"]  # untouched defaults survive
    assert cfg["clips"] == {**DEFAULTS["clips"], "max_sec": 45}
    rules = tmp_path / "r.txt"
    rules.write_text("# comment\n\nDON'T swear.\n", encoding="utf-8")
    assert load_campaign_rules(rules) == "DON'T swear."
    assert load_campaign_rules(tmp_path / "missing.txt") == ""


def test_trim_window_adds_head_and_tail_room_and_clamps():
    raw = {"offset_sec": 99.5}
    assert trim_window(raw, {"start_sec": 100.0, "end_sec": 130.0}, 31.0) == pytest.approx((0.4, 30.85))
    assert trim_window({"offset_sec": 100.0}, {"start_sec": 100.0, "end_sec": 130.0}, 30.2) == pytest.approx((0.0, 30.2))


def test_manifest_carries_source_t0_and_empty_marks(cfg):
    clip = {"id": 1, "title": "T", "hook_line": "H", "reason": "r", "score": 7.0, "score_breakdown": {},
            "heatmap": 0.0, "selected_by": "llm", "start_sec": 100.0, "end_sec": 130.0, "text": "words"}
    transcript = {"source": "auto", "lang": "en", "has_word_timings": True, "heatmap": []}
    results = {1: {"status": "ok", "file": "01_t.mp4", "source_t0": 99.9}}
    m = build_manifest({"id": "vid", "title": "t"}, transcript, [clip], results, {"model": "m", "usage": None},
                       cfg, "", "https://youtu.be/vid", rendered=True)
    assert m["clips"][0]["source_t0"] == 99.9
    assert m["clips"][0]["review"] == {"approved": None, "notes": "", "marks": [], "trims": []}
